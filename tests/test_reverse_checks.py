"""S3 的**反向验证**固化：注入缺陷 → 断言已有的某条测试真的会红。

**为什么要一个文件专门干这件事**：正着跑测试只能证明"现在的代码是绿的"，
不能证明"测试守得住这条约束"。S3 实施期间我在 `/tmp` 里手跑了 15 条反向验证，
其中一条当场暴露了一个假测试（`assert GIT_TIMEOUT_SECONDS > 0` 把超时改成 0 也还是绿）。
手工跑在 `/tmp` 里 = 不可复现、下一个人不会跑 —— 所以固化进来（06-pitfalls T23 的同一教训）。

## 实现方式（为什么不 monkeypatch）

一条反向验证的语义是「**改掉实现文件的那一行**，然后跑**另一条测试**看它红」。要做到这件事，
候选方案只有两种：

- **同进程 monkeypatch**：那些测试要跑真实 git，拆成可 patch 的形态等于改测试本身；
  也 patch 不到"删掉一行代码"这种缺陷。
- **子进程里跑真实 pytest**（采用）：慢一点，但验证的是真的那条测试，注入的也是真的代码改动。

所以这里：把源码文件复制到 `tmp_path`、文本替换掉关键片段、用 `PYTHONPATH` 指向副本跑
真实 pytest、断言退出码非零（测试变红）、最后校验**副本**是不是真的被改了、**原文件**有没有被动。

## 口径与预算

每条是一个真实 pytest 进程，约 0.2–0.8 秒（实测整套 ~4 秒；`gw` 那条要起内核，约 0.5 秒）。
远低于"不许让运行时间翻倍"的红线。如果将来变慢，先合并断言再考虑加 marker ——
**不要**删条目，删了这条防线就没了。

## 一个必须记住的陷阱（T30）

`tests/gateway_util.py:Kernel` 起的是 `.venv/bin/postel-studio`，它装载的是**已安装**的包、
而且显式 `env.pop("PYTHONPATH", None)`。所以：

- 用 `PYTHONPATH` 指向副本**改不到那个内核**；
- 反过来，任何"改网关 → 端到端测试变红"的想法在这里都不成立。

要让**真内核**也读到副本，得用 `python -m pytest`（`-m` 会把 cwd 放进 `sys.path[0]`）。
第二条注入（`repo.bind` 未注册）因此改成跑**进程内 app** 的测试，而不是端到端那条。

S5 的 5 条注入（D12 表）跨 L0 / L3 / 前端三层：`DIFF_FLAGS` 的两个 flag、`run_git` 的
缺省 `decode_errors`、`resolve_commit` 的 `^{commit}`、以及页面里 diff 行的渲染方式。
S7 的 4 条注入针对**写面与状态机**：路径白名单、复用判定、`--force`、baseRef 形状。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src"
# 跑被测测试时只给它**一个**测试节点，别让它跑整个套件（会套娃、也会超时）
RUN_TIMEOUT_SECONDS = 120


def _target_test(name: str, *, file: str = "test_bind") -> str:
    """被测测试的节点 id。

    缺省在 `tests/test_bind.py`（S3 的验收文件）；S4 的分支列表注入指向
    `tests/test_branches.py`，S7 的工作区注入指向 `tests/test_worktree.py`。
    """
    return f"tests/{file}.py::{name}"


# (注入点文件, 原始片段, 替换成什么, 应当变红的测试, 缺陷说明)
INJECTIONS: tuple[tuple[str, str, str, str, str], ...] = (
    # ---- 05 §3.3 / D5：子进程 ----
    (
        "postel_studio/gitops/repo.py",
        'env["GIT_TERMINAL_PROMPT"] = "0"',
        "pass",
        _target_test("test_bind_uses_git_in_a_way_that_cannot_prompt"),
        "去掉 GIT_TERMINAL_PROMPT=0（git 会挂住等密码）",
    ),
    (
        "postel_studio/gitops/repo.py",
        'env["GIT_CONFIG_NOSYSTEM"] = "1"',
        "pass",
        _target_test("test_bind_uses_git_in_a_way_that_cannot_prompt"),
        "去掉 GIT_CONFIG_NOSYSTEM=1（用户机器配置会影响结果）",
    ),
    (
        "postel_studio/gitops/repo.py",
        "GIT_TIMEOUT_SECONDS: Final = 5.0",
        "GIT_TIMEOUT_SECONDS: Final = 0.0",
        _target_test("test_bind_uses_git_in_a_way_that_cannot_prompt"),
        "超时设成 0（等于没有超时）",
    ),
    (
        "postel_studio/gitops/repo.py",
        "            shell=False,",  # 带缩进：只命中 run_git 里的那处调用
        "            shell=True,",
        _target_test("test_git_subprocess_shell_is_disabled"),
        "把 shell=False 改成 True（05 §3.3 禁止）",
    ),
    # ---- 05 §4.2 / D6：错误不回显用户输入 ----
    (
        "postel_studio/api/repository.py",
        "raise _fail(NOT_A_REPO, MSG_NOT_A_REPO)",
        "raise _fail(NOT_A_REPO, MSG_NOT_A_REPO + str(target))",
        _target_test("test_not_a_repo_message_does_not_echo_any_user_path"),
        "错误 message 里拼上用户路径（05 §4.2 违规）",
    ),
    (
        "postel_studio/api/repository.py",
        "        if not Path(raw).is_absolute():\n            raise _fail(BAD_CWD, MSG_BAD_CWD)",
        "        if False:\n            pass",
        _target_test("test_bind_relative_path_is_bad_cwd"),
        "不拒绝相对路径（D4）",
    ),
    (
        "postel_studio/api/repository.py",
        "    if not target.is_dir():\n        return None",
        "    if not target.is_dir():\n        return target",
        _target_test("test_bind_file_path_is_not_a_repo"),
        "把文件路径当成仓库根",
    ),
    # ---- D2：子目录校正（「用户以为绑了 A 其实绑了 B」）----
    (
        "postel_studio/api/repository.py",
        "        root = next(\n"
        "            (ancestor for ancestor in target.parents"
        " if gitops.is_worktree_root(ancestor)),\n"
        "            None,\n"
        "        )",
        "        root = target",  # 注入：不做校正，直接把子目录当根
        _target_test("test_bind_subdirectory_stores_repo_root_not_the_subdirectory"),
        "去掉子目录校正（D2）",
    ),
    (
        "postel_studio/api/repository.py",
        '    if any(part == ".git" for part in relative.parts):\n        return True',
        '    if target.name == ".git":\n        return True',
        _target_test("test_bind_path_inside_the_git_directory_is_not_a_repo"),
        "T29：只判尾段 .git，`.git/objects` 会逃逸",
    ),
    # ---- D3：repo_id ----
    # T29/T31 是一对孪生缺陷：拒绝得太少 → `.git/objects` 逃逸；拒绝得太多 → 误杀
    # "父目录恰好叫 .git" 的合法仓库。两条都要有锁，只锁一边等于没锁。
    (
        "postel_studio/api/repository.py",
        "    relative = target.relative_to(root)\n"
        '    if any(part == ".git" for part in relative.parts):\n'
        "        return True",
        "    return False",  # 注入：T29 那条判定整体失效（`.git` 内部不再被拒）
        _target_test("test_bind_path_inside_the_git_directory_is_not_a_repo"),
        "T29：删掉 `.git` 判定",
    ),
    (
        "postel_studio/api/repository.py",
        "    relative = target.relative_to(root)\n"
        '    if any(part == ".git" for part in relative.parts):\n'
        "        return True",
        '    return any(part == ".git" for part in target.parts)',  # 注入：看整条绝对路径
        _target_test("test_repo_root_whose_parent_is_named_dot_git_still_binds"),
        "T31：把 `.git` 判定改成「整条绝对路径任意段 = .git」→ 误杀合法仓库",
    ),
    (
        "postel_studio/domain/models.py",
        "REPO_ID_LENGTH: Final = 12",
        "REPO_ID_LENGTH: Final = 16",
        _target_test("test_bind_regular_repo_returns_hex12_id_name_and_main"),
        "repo_id 长度改成 16（06 §2.1 / 07 §4.1 要求 12）",
    ),
    (
        "postel_studio/domain/models.py",
        'return f"{host}/{path}" if path else host',
        "return url",
        _target_test("test_https_and_ssh_origins_share_the_same_repo_id"),
        "origin 不归一化（https 与 ssh 会得不同 id）",
    ),
    # ---- 05 §4.1：state.db 权限 ----
    (
        "postel_studio/storage/db.py",
        "_chmod(path_for_chmod, 0o600)",
        "pass",
        _target_test("test_data_survives_reopening_the_store"),
        "不收紧 state.db 的 0600 权限",
    ),
    # ---- D6：错误码 ----
    (
        "postel_studio/api/repository.py",
        "            # D5：git 不存在**不崩内核**，回 503 类码；S19 的 doctor 负责告诉用户怎么装\n"
        "            raise _fail(ADAPTER_UNAVAILABLE, MSG_ADAPTER_UNAVAILABLE) from None",
        "            raise _fail(frames.INTERNAL, MSG_GIT_TIMEOUT) from None",
        _target_test("test_missing_git_is_adapter_unavailable"),
        "ADAPTER_UNAVAILABLE 翻成 INTERNAL",
    ),
    # ---- D1/S3 方法表：WS 帧 ----
    # 注意跑的是**进程内 app**（`create_app()` + TestClient），不是 `gateway_util.Kernel` 起的
    # 真内核子进程——后者用站点包装载已安装的包、且 `env.pop("PYTHONPATH")`，看不到副本。
    # 见 `test_bind.py::test_kernel_subprocess_ignores_pythonpath_like_gateway_util_does`。
    (
        "postel_studio/api/gateway.py",
        '    if method == "repo.bind":  # 07 §2.2 / 07 §4.1；S3 D1：走帧，不开 HTTP 路由\n'
        "        return providers.repo_bind(params)",
        "    if False:\n        return None",
        _target_test("test_inprocess_dispatch_exposes_repo_bind"),
        "网关不注册 repo.bind",
    ),
    # ---- S4：分支列表（D1/D2/D3/D5 + T33）----
    (
        "postel_studio/api/repository.py",
        "        except gitops.GitUnavailableError:\n"
        "            raise _fail(ADAPTER_UNAVAILABLE, MSG_ADAPTER_UNAVAILABLE) from None",
        "        except gitops.GitUnavailableError:\n"
        "            raise _fail(frames.INTERNAL, MSG_GIT_TIMEOUT) from None",
        _target_test(
            "test_missing_git_executable_maps_to_adapter_unavailable", file="test_branches"
        ),
        "repo.branches 把 ADAPTER_UNAVAILABLE 翻成 INTERNAL",
    ),
    (
        "postel_studio/gitops/repo.py",
        'is_current=marker == "*"',
        "is_current=bool(fields[3])",
        _target_test("test_branch_current_flag_is_only_on_the_head_branch", file="test_branches"),
        "T33：当前判定用未 strip 的 marker 做真值判断（非当前分支的 marker 是空格）",
    ),
    (
        "postel_studio/gitops/repo.py",
        "                upstream=upstream or None,",
        "                upstream=upstream,",
        _target_test(
            "test_branch_upstream_is_short_name_and_none_when_missing", file="test_branches"
        ),
        "D1：没有 upstream 时不再把空串归一成 None",
    ),
    (
        "postel_studio/gitops/repo.py",
        'run_git(["for-each-ref", "refs/heads/", f"--format={BRANCH_FORMAT}"], root=root)',
        'run_git(["for-each-ref", f"--format={BRANCH_FORMAT}"], root=root)',
        _target_test("test_only_local_branches_are_listed_after_push", file="test_branches"),
        "D2：不限定 refs/heads/（remote-tracking 会混进分支列表）",
    ),
    # ---- S5：diff（D4 / D2 / D3.2 / D9）----
    (
        "postel_studio/gitops/repo.py",
        '    "--no-color",\n',
        "",
        _target_test("test_patch_has_no_ansi_when_color_ui_always", file="test_diff"),
        "D12 #1：DIFF_FLAGS 去掉 --no-color（color.ui=always 时 patch 混入 ANSI）",
    ),
    (
        "postel_studio/gitops/repo.py",
        '    "--no-ext-diff",\n',
        "",
        _target_test("test_patch_ignores_external_diff_driver", file="test_diff"),
        "D12 #2：DIFF_FLAGS 去掉 --no-ext-diff（diff.external 会让 patch 失败）",
    ),
    (
        "postel_studio/gitops/repo.py",
        'decode_errors: str = "strict"',
        'decode_errors: str = "replace"',
        _target_test("test_branches_non_utf8_refname_is_not_supported", file="test_diff"),
        "D12 #3：run_git 缺省宽松解码（非 UTF-8 refname 被 mangled 回传而不是 NOT_SUPPORTED）",
    ),
    (
        "postel_studio/gitops/repo.py",
        'run_git(["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], root=root)',
        'run_git(["rev-parse", "--verify", "--end-of-options", ref], root=root)',
        _target_test("test_bad_refs_are_bad_ref", file="test_diff"),
        "D12 #4：解析 ref 去掉 ^{commit}（tree id 不再被拒）",
    ),
    (
        "postel_studio/web/assets.py",
        "      row.textContent = lines[i];     // 不可信文本：只进 textContent\n",
        "      row.innerHTML = lines[i];\n",
        _target_test("test_page_never_uses_inner_html", file="test_diff"),
        "D12 #5：diff 行渲染 textContent 改成 innerHTML（不可信文本进 HTML）",
    ),
    # ---- S6：Bot 惰性播种 / charter 记忆空间 / 页面渲染（D12 的 5 条）----
    (
        "postel_studio/storage/paths.py",
        "    if not charter.exists():  # D3：已存在就不动，绝不覆盖用户编辑\n"
        '        atomic_write(charter, CHARTER_TEMPLATE.format(name=name).encode("utf-8"))',
        '    atomic_write(charter, CHARTER_TEMPLATE.format(name=name).encode("utf-8"))',
        _target_test(
            "test_ensure_memory_space_never_overwrites_an_existing_charter",
            file="test_bot_charter",
        ),
        "S6 #1：ensure_memory_space 每次都用模板覆盖 charter.md（D2/D3 幂等被破坏）",
    ),
    (
        "postel_studio/storage/paths.py",
        '        atomic_write(charter, CHARTER_TEMPLATE.format(name=name).encode("utf-8"))',
        '        charter.write_text(CHARTER_TEMPLATE.format(name=name), encoding="utf-8")',
        _target_test("test_ensure_memory_space_layout_and_permissions", file="test_bot_charter"),
        "S6 #2：charter 写入不用 atomic_write 而用 write_text（0644，非原子；T38）",
    ),
    (
        "postel_studio/storage/paths.py",
        "        path.mkdir(parents=True, exist_ok=True)\n"
        "        _chmod(path, DIR_MODE)  # 显式收紧：umask 让 mkdir 的默认 mode 变松（T38）",
        "        path.mkdir(parents=True, exist_ok=True)",
        _target_test("test_ensure_memory_space_layout_and_permissions", file="test_bot_charter"),
        "S6 #3：每层建完不 chmod（umask 022 下父级是 0755；T38）",
    ),
    (
        "postel_studio/storage/paths.py",
        "    if not bot_name_ok(name):\n"
        '        raise ValueError(f"bot 名字不合法：{name!r}")\n'
        "    path = bots_root() / repo_id / name",
        "    path = bots_root() / repo_id / name",
        _target_test(
            "test_ensure_memory_space_rejects_names_that_escape_bots_root",
            file="test_bot_charter",
        ),
        "S6 #4：进路径前不校验 bot 名字白名单（`..` 恰好 resolve 回 bots 根，越界断言拦不住）",
    ),
    (
        "postel_studio/web/assets.py",
        "      charterContent.textContent ="
        ' String(payload.content == null ? "" : payload.content);',
        '      charterContent.innerHTML = String(payload.content == null ? "" : payload.content);',
        _target_test("test_page_never_uses_inner_html_in_bot_panels", file="test_bot_charter"),
        "S6 #5：charter 全文从 textContent 改成 innerHTML（用户可编辑文本进 HTML）",
    ),
    # ---- S7：工作区的写面与状态机（D2 / D3 / D5 / D7）----
    (
        "postel_studio/storage/paths.py",
        "    if not bot_name_ok(name):\n"
        '        raise ValueError(f"bot 名字不合法：{name!r}")\n'
        "    if not _REPO_ID_RE.fullmatch(repo_id):",
        "    if not _REPO_ID_RE.fullmatch(repo_id):",
        _target_test(
            "test_worktree_dir_rejects_names_that_escape_worktrees_root", file="test_worktree"
        ),
        "S7 #1：`worktree_dir` 不再校验 bot 名白名单（`../x` 恰好 resolve 回根，越界断言拦不住）",
    ),
    (
        "postel_studio/api/worktrees.py",
        "            if registered is not None and not registered.prunable and target.is_dir():",
        "            if False:  # 注入：不认已有工作区，每次都想新建",
        _target_test("test_acquire_twice_reuses_the_same_worktree", file="test_worktree"),
        "S7 #2：acquire 不再复用已注册的工作区（重复点击会去撞 git 的'路径已存在'）",
    ),
    (
        "postel_studio/gitops/worktrees.py",
        '    _run(["worktree", "remove", str(target)], root=root)',
        '    _run(["worktree", "remove", "--force", str(target)], root=root)',
        _target_test(
            "test_release_dirty_worktree_is_conflict_and_keeps_files", file="test_worktree"
        ),
        "S7 #3：回收加 `--force`（未提交的改动会被删掉，D5 的红线）",
    ),
    (
        "postel_studio/api/worktrees.py",
        "        if not isinstance(value, str) or not ref_name_ok(value):",
        "        if not isinstance(value, str):",
        _target_test("test_acquire_rejects_invalid_base_ref_shape", file="test_worktree"),
        "S7 #4：baseRef 不再过 S5 D3.1 的形状（`-x` 会进 git 的选项位，用户输入直接下传）",
    ),
    # ---- S8：建 Bot 的四条硬约束 + 一条页面契约 ----
    (
        "postel_studio/api/bots.py",
        "        if isinstance(raw, str) and raw in UNASSIGNABLE_PERMISSIONS:\n"
        "            raise _fail(NOT_SUPPORTED, MSG_PERMISSION_UNSUPPORTED)",
        "        pass",
        _target_test("test_create_rejects_l3_and_l4", file="test_bot_create"),
        "S8 #1：`L3`/`L4` 不再被单独拒绝（会掉进 INVALID_PARAMS——码不再说清'有但不给'）",
    ),
    (
        "postel_studio/domain/models.py",
        'BOT_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9-]{0,31}$")',
        'BOT_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9-]{0,99}$")',
        _target_test("test_create_rejects_illegal_names", file="test_bot_create"),
        "S8 #2：名字白名单放宽到 99 字（超长名字被接受并真的建出目录）",
    ),
    (
        "postel_studio/domain/models.py",
        "BOT_PERMISSION_DEFAULT: Final = Permission.L1",
        "BOT_PERMISSION_DEFAULT: Final = Permission.L0",
        _target_test("test_create_defaults_to_l1", file="test_bot_create"),
        "S8 #3：新建 Bot 的默认级别从 L1 掉到 L0（05 §2.2 的默认值被改掉）",
    ),
    (
        "postel_studio/api/bots.py",
        "        repo = self._require_repo(params)\n"
        "        return [self._split(bot) for bot in self._store.list_for_repo(repo.repo_id)]",
        "        repo = self._require_repo(params)\n"
        "        found = self._store.list_for_repo(repo.repo_id)\n"
        "        if not found:\n"
        "            ensure_memory_space(repo.repo_id, 'seeded')\n"
        "            found = [\n"
        "                self._store.insert(\n"
        "                    Bot(\n"
        "                        bot_id=bot_id(repo.repo_id, 'seeded'),\n"
        "                        repo_id=repo.repo_id,\n"
        "                        name='seeded',\n"
        "                        permission='L1',\n"
        "                        agent_kind=None,\n"
        "                        charter_rev=0,\n"
        "                        created_at=0.0,\n"
        "                    )\n"
        "                )\n"
        "            ]\n"
        "        return [self._split(bot) for bot in found]",
        _target_test("test_list_on_a_repo_with_no_bots_writes_nothing", file="test_bot_create"),
        "S8 #4：惰性播种复活（一个 GET 又开始落行 + 建目录，U17 第一半被推翻）",
    ),
    (
        "postel_studio/web/assets.py",
        '        <option value="L2+">L2+ · 开本仓库 PR</option>\n'
        "      </select>\n"
        '      <div id="bot-create-actions">',
        '        <option value="L2+">L2+ · 开本仓库 PR</option>\n'
        '        <option value="L3">L3 · 跨仓库 PR</option>\n'
        "      </select>\n"
        '      <div id="bot-create-actions">',
        _target_test(
            "test_page_permission_select_lists_exactly_four_levels", file="test_bot_create"
        ),
        "S8 #5：新建表单的下拉多出 L3（产品红线：第一版永不授予 L3/L4）",
    ),
    # ---- S9：握手的两条硬约束（能力必须算、子进程必须回收）----
    (
        "postel_studio/agent/acp_client.py",
        "    return tuple(sorted(flags))",
        '    return ("loadSession",)  # 注入：硬编码一份"看起来对"的能力',
        _target_test("test_flags_are_derived_not_hardcoded", file="test_acp_detect"),
        "S9 #1：能力改成写死的列表（界面看起来一样，但 Agent 升级后永远不会变）",
    ),
    (
        "postel_studio/agent/acp_client.py",
        "    proc.terminate()",
        "    return  # 注入：不杀，留着孤儿",
        _target_test("test_child_is_reaped_after_timeout", file="test_acp_detect"),
        "S9 #2：超时后不再回收子进程（每点一次检测漏一个 opencode 进程）",
    ),
    # ---- S10：派活的两道闸（权限判定、BUSY 双锁）----
    (
        "postel_studio/orchestrator.py",
        "        if not permission_covers(bot.permission, DISPATCH_MIN_PERMISSION):"
        "  # D4：先于一切执行",
        "        if False:  # 注入：权限判定整段跳过（L0 也放行）",
        _target_test("test_l0_bot_refused_before_any_execution", file="test_dispatch"),
        "S10 #1：dispatch 跳过权限判定（05 §2.1：判定必须先于执行，且无第二路径）",
    ),
    # S10 #2 是**组合注入**（见下方 COMPOSITE_INJECTIONS）：BUSY 有两道锁（D5），只拆
    # 其中一道时另一道必须独立兜住——所以"单拆检查"的注入**不该**让测试红；两道一起拆才红。
    # ---- S11：候选的四条红线 ----
    (
        "postel_studio/domain/models.py",
        '    safe_name = _BRANCH_SAFE.sub("-", name.lower()) or "unknown"',
        "    safe_name = name.lower() or 'unknown'  # 注入：分支名不再清洗",
        _target_test(
            "test_candidate_branch_name_is_predictable_and_sanitized", file="test_candidate"
        ),
        "S11 #1：候选分支名直接用 Bot 名（坏数据能拼出非法 refname）",
    ),
    (
        "postel_studio/orchestrator.py",
        "            base_sha = self._candidates.snapshot_base(path)"
        "  # S11 D2：会话之前，复用也不串任务",
        '            base_sha = ""  # 注入：会话前不快照 HEAD',
        _target_test("test_second_task_on_reused_worktree_diff_is_isolated", file="test_candidate"),
        "S11 #2：复用工作区时不再隔离每个任务的 diff（D2）",
    ),
    # S11 #3 是组合注入（见 COMPOSITE_INJECTIONS）：no-change 有两道守卫，
    # 只拆一道时另一道兜住——与 S10 #2 的 BUSY 双锁同一个教训（T45）。
    (
        "postel_studio/orchestrator.py",
        "            self._candidates.discard(candidate.candidate_id)",
        "            pass  # 注入：竞态输家不删刚插入的候选",
        _target_test(
            "test_transition_loser_discards_just_inserted_candidate", file="test_candidate"
        ),
        "S11 #4：cancel 赢了竞态还留下指向已取消任务的候选行（D6）",
    ),
    # ---- S12：收件箱的三条红线（页面是 assets.py 里的静态字符串）----
    (
        "postel_studio/web/assets.py",
        (
            '        inboxEl.dataset.state = "empty";\n'
            '        inboxState.textContent = "暂无待审变更";'
        ),
        '        inboxEl.dataset.state = "ready";  # 注入：空白',
        _target_test(
            "test_empty_repo_returns_no_rows_and_page_has_empty_state_text",
            file="test_inbox_list",
        ),
        "S12 #1：0 条候选时不再显示空态文案（空白被误认为页面坏了）",
    ),
    (
        "postel_studio/web/assets.py",
        '      inboxLink.className = "repo-inbox-link";',
        '      inboxLink.className = "repo-inbox-link hidden-tab";  # 注入：入口伪装成 tab',
        _target_test("test_no_new_top_level_tab_and_entry_uses_repo_row", file="test_inbox_list"),
        "S12 #2：收件箱入口不再是仓库条目下的链接（Q1 D1）",
    ),
    (
        "postel_studio/web/assets.py",
        '    if (filter !== "all") { params.reviewState = filter; }',
        "    // 注入：忽略筛选，服务端永远返回全部",
        _target_test("test_review_state_filter_returns_only_matching_rows", file="test_inbox_list"),
        "S12 #3：筛选不再发给 candidate.list（D3：服务端是筛选的唯一事实源）",
    ),
    # ---- S13：审核的三条红线 ----
    (
        "postel_studio/domain/models.py",
        """    allowed = REVIEW_TRANSITIONS.get(current)
    return allowed is not None and target in allowed""",
        """    return True  # 注入：状态机永远放行""",
        _target_test("test_review_transition_matrix", file="test_review"),
        "S13 #1：审核状态机永远放行（rejected→approved 也通过）",
    ),
    (
        "postel_studio/storage/db.py",
        """            cursor = self._conn.execute(
                "UPDATE candidates SET review_state = ?, reject_reason = ?"
                " WHERE candidate_id = ? AND review_state = ?",
                (target, reject_reason, candidate_id, current),
            )""",
        """            cursor = self._conn.execute(
                "UPDATE candidates SET review_state = ? WHERE candidate_id = ?",
                (target, candidate_id),
            )  # 注入：无 WHERE 状态门、理由不写""",
        _target_test("test_transition_loser_does_not_overwrite_reason", file="test_review"),
        "S13 #2：迁移丢了状态 WHERE 与理由（竞态输家覆盖赢家）",
    ),
    (
        "postel_studio/web/assets.py",
        '        <button id="candidate-reject" type="button">拒绝并说明</button>',
        '        <button id="candidate-merge" type="button">合并</button>',
        _target_test("test_no_merge_button_anywhere", file="test_review"),
        "S13 #3：页面出现「合并」按钮（L4 永不开放给 Bot）",
    ),
)


# S3.5 · U9 的**组合注入**（一次改两处；结构同 INJECTIONS，只是 patches 允许多项）。
#
# U9 要两处同时存在才复现（实测，见 06-pitfalls T32）：(a) id 依赖 origin（S3 的算法）**且**
# (b) `upsert` 按 path 命中时覆盖 `repo_id`。只注入任一处，验收 2/3 都是**绿的**——只有 (b)
# 时新算法算出的 id 本来就相同；只有 (a) 时规则 1 不覆盖 id。单独两半各写成"必须仍然绿"的
# 用例（`_ONLY_*`）。能红的只有 (a)+(b) = S3 的真实代码形态。
#
# 注意 (b) 的参数元组必须一起改：只改 SQL 会红在 `ProgrammingError` 上，那是"红错原因"
# （见 `_RUNTIME_BREAKAGES`）。
_UPSERT_COVERS_ID: tuple[tuple[str, str, str], ...] = (
    (
        "postel_studio/storage/db.py",
        '                    "UPDATE repos SET origin_url = ?, origin_hash = ?,'
        ' default_branch = ?,"',
        '                    "UPDATE repos SET repo_id = ?, origin_url = ?, origin_hash = ?,"\n'
        '                    " default_branch = ?,"',
    ),
    (
        "postel_studio/storage/db.py",
        '                    " added_at = ? WHERE path = ?",\n                    (\n'
        "                        repo.origin_url,",
        '                    " added_at = ? WHERE path = ?",\n                    (\n'
        "                        repo.repo_id,\n"
        "                        repo.origin_url,",
    ),
)


# U9(a)：调用方把 `repo_id()` 换回「有 origin 用 URL 哈希」。import + 调用点两段必须一起注入。
_OLD_ID_ALGORITHM: tuple[tuple[str, str, str], ...] = (
    (
        "postel_studio/api/repository.py",
        "from postel_studio.domain.models import Repo, origin_hash, ref_name_ok, repo_id",
        "from postel_studio.domain.models import ("
        "Repo, origin_hash, ref_name_ok, repo_id, repo_id_for_origin)",
    ),
    (
        "postel_studio/api/repository.py",
        "                repo_id=repo_id(str(root)),",
        "                repo_id=repo_id_for_origin(origin) if origin else repo_id(str(root)),",
    ),
)

# S11 #3：两道「无事可做」守卫必须一起拆，单独拆任一道测试仍是绿的（T45）。
_S11_NO_CHANGE_GUARDS: tuple[tuple[str, str, str], ...] = (
    (
        "postel_studio/candidates.py",
        "        if not dirty and head == base_sha:\n            return None",
        "        if False:\n            return None",
    ),
    (
        "postel_studio/candidates.py",
        "        if head == base_sha:\n            return None",
        "        if False:\n            return None",
    ),
    (
        "postel_studio/candidates.py",
        "        if stat is None or stat.files_changed == 0:\n            return None",
        "        if False:\n            return None",
    ),
)

COMPOSITE_INJECTIONS: tuple[tuple[tuple[tuple[str, str, str], ...], str, str], ...] = (
    (
        _S11_NO_CHANGE_GUARDS,
        _target_test(
            "test_no_change_task_stays_succeeded_without_candidate", file="test_candidate"
        ),
        "S11 #3（组合）：两道 no-change 守卫一起拆 → 空任务不得仍无候选",
    ),
    (
        _OLD_ID_ALGORITHM + _UPSERT_COVERS_ID,
        _target_test("test_rebinding_after_origin_added_updates_the_row"),
        "U9 完整形态（旧算法 + upsert 覆盖 id）：验收 2 的用例必须红",
    ),
    (
        _OLD_ID_ALGORITHM + _UPSERT_COVERS_ID,
        _target_test("test_unbound_legacy_id_is_still_reachable_after_origin_change"),
        "U9 完整形态：验收 3 的用例必须红",
    ),
    (
        _OLD_ID_ALGORITHM + _UPSERT_COVERS_ID,
        _target_test("test_binding_an_existing_v1_row_keeps_its_legacy_id"),
        "U9 完整形态：v1 老行的 id 在下一次 bind 被覆盖（D3 的留痕被抹掉）",
    ),
    # ---- S10 #2：BUSY 的两道锁一起拆（D5）----
    # 只拆 orchestrator 的检查时，`tasks_active_per_bot` 唯一索引照样拦（上面 S10 #2 注释
    # 里的单条注入已实测：目标测试保持绿——这正是"两道锁互为后备"的证据）。两道一起拆，
    # 第二次 dispatch 必然插进活跃行 → 目标测试必须红。
    (
        (
            (
                "postel_studio/orchestrator.py",
                "        if self._tasks.active_for_bot(bot.bot_id) is not None:",
                "        if False:  # 注入：活跃任务检查跳过",
            ),
            (
                "postel_studio/storage/db.py",
                "    CREATE UNIQUE INDEX tasks_active_per_bot ON tasks (bot_id)"
                " WHERE state IN ('queued', 'running');",
                "    -- 注入：唯一索引拆掉",
            ),
        ),
        _target_test("test_second_dispatch_while_active_is_busy", file="test_dispatch"),
        "S10 #2（组合）：检查 + 唯一索引一起拆 → 第二刀并发派活必须被拒（测试红）",
    ),
)


@pytest.fixture(scope="module")
def patched_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """一份可写的 `src` 副本（每次注入都在它上面改，原 `source_root` 只读）。"""
    root = tmp_path_factory.mktemp("reverse-src")
    shutil.copytree(SOURCE_ROOT, root / "src")
    return root / "src"


def _snapshot(source: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(source)): path.read_bytes()
        for path in sorted(source.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def _run_target_test(source: Path, node: str) -> subprocess.CompletedProcess[str]:
    """用**副本**源码跑一条真实 pytest。`PYTHONPATH` 前置副本，项目根保留在后。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(source), str(REPO_ROOT)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # 别往副本里写 __pycache__（会污染快照对比）
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--no-cov",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            node,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_SECONDS,
        check=False,
    )


def test_sanity_the_copy_is_really_what_runs(patched_source: Path) -> None:
    """**前提**：副本源码真的会被 import 到（否则下面所有"变红"都可能来自别的原因）。

    验证办法：在副本里把一个模块常量改成一个哨兵值，读回来必须是哨兵。
    这条要是红了，说明 `PYTHONPATH` 没起作用——那么 INJECTIONS 里的"变红"全部不可信。
    """
    module = patched_source / "postel_studio" / "domain" / "models.py"
    original = module.read_text(encoding="utf-8")
    try:
        module.write_text(
            original.replace("REPO_ID_LENGTH: Final = 12", "REPO_ID_LENGTH: Final = 77"),
            encoding="utf-8",
        )
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "from postel_studio.domain.models import REPO_ID_LENGTH;"
                "print(REPO_ID_LENGTH);"
                "import postel_studio;print(postel_studio.__file__)",
            ],
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join([str(patched_source), str(REPO_ROOT)]),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert probe.returncode == 0, probe.stderr
        first, imported = probe.stdout.split()
        assert first == "77", f"副本没生效：读到的是 {first}"
        assert imported.startswith(str(patched_source)), f"import 到的是 {imported}"
    finally:
        module.write_text(original, encoding="utf-8")


def _apply_patches(source: Path, patches: tuple[tuple[str, str, str], ...]) -> dict[str, str]:
    """把一组补丁打到**副本**上，返回「文件 → 原始内容」（供 `finally` 复原）。

    两条纪律（S3.5 加的第一条）：

    1. **锚点必须恰好命中一次**：`str.count(old)` 不是 1 就当场失败。S3.5 改 `db.py` 时它当场
       抓到过一条旧锚点静默失配（`UPDATE` 里不再有 `repo_id`，那条注入会退化成空操作而永远绿）。
    2. 同一文件的多段补丁**按顺序叠加**（后一段在前一段的结果上找锚点）。
    """
    pristine: dict[str, str] = {}
    for file, old, new in patches:
        path = source / file
        if file not in pristine:
            pristine[file] = path.read_text(encoding="utf-8")
        text = path.read_text(encoding="utf-8")
        hits = text.count(old)
        assert hits == 1, f"{file} 里锚点命中 {hits} 次（应恰好 1 次）：{old[:60]!r}"
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return pristine


def _restore_patches(source: Path, pristine: dict[str, str]) -> None:
    """复原副本（**必须**在 `finally` 里调：下一个参数化用例还要用同一份副本）。"""
    for file, text in pristine.items():
        (source / file).write_text(text, encoding="utf-8")


# 注入后目标测试"红"的**成因**不能是这些：它们说明"注入把代码弄坏了"，与"这条约束有没有被
# 守住"无关。实测踩过一次：只改 UPDATE 的 SQL 而忘了补参数元组，测试红在
# `sqlite3.ProgrammingError: Incorrect number of bindings supplied` 上——那次"红"什么也没证明。
_RUNTIME_BREAKAGES: tuple[str, ...] = (
    "ProgrammingError",
    "OperationalError",
    "NameError",
    "AttributeError",
    "ImportError",
    "ModuleNotFoundError",
    "TypeError",
)


def _assert_red_for_the_right_reason(
    result: subprocess.CompletedProcess[str], test_node: str, description: str
) -> None:
    """目标测试必须**红**，且红在断言上（不是注入把代码改坏了）。"""
    assert result.returncode != 0, (
        f"注入「{description}」之后 {test_node} **仍然是绿的**——这条测试守不住这个约束。\n"
        f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-1000:]}"
    )
    output = result.stdout + result.stderr
    for breakage in _RUNTIME_BREAKAGES:
        assert breakage not in output, (
            f"注入「{description}」之后目标测试红在 {breakage} 上——注入后的代码没跑起来，"
            f"这次变红**不说明**那条约束被守住。\nstdout:\n{result.stdout[-1500:]}"
        )


@pytest.mark.parametrize(
    ("file", "old", "new", "test_node", "description"),
    INJECTIONS,
    ids=[item[4] for item in INJECTIONS],
)
def test_injected_defect_turns_the_target_test_red(
    patched_source: Path,
    file: str,
    old: str,
    new: str,
    test_node: str,
    description: str,
) -> None:
    """注入缺陷 → 目标测试**必须**变红；同时断言副本真的被改了、原文件没被动。"""
    pristine = _apply_patches(patched_source, ((file, old, new),))
    try:
        result = _run_target_test(patched_source, test_node)
        _assert_red_for_the_right_reason(result, test_node, description)
    finally:
        _restore_patches(patched_source, pristine)


@pytest.mark.parametrize(
    ("patches", "test_node", "description"),
    COMPOSITE_INJECTIONS,
    ids=[item[2] for item in COMPOSITE_INJECTIONS],
)
def test_composite_injected_defect_turns_the_target_test_red(
    patched_source: Path,
    patches: tuple[tuple[str, str, str], ...],
    test_node: str,
    description: str,
) -> None:
    """一次改两处（U9 必须如此，见 `COMPOSITE_INJECTIONS` 的注释）→ 目标测试**必须**变红。"""
    pristine = _apply_patches(patched_source, patches)
    try:
        result = _run_target_test(patched_source, test_node)
        _assert_red_for_the_right_reason(result, test_node, description)
    finally:
        _restore_patches(patched_source, pristine)


@pytest.mark.parametrize(
    ("patches", "label"),
    [
        (_OLD_ID_ALGORITHM, "只有 (a)：算法退回「有 origin 用 URL 哈希」"),
        (_UPSERT_COVERS_ID, "只有 (b)：upsert 按 path 命中时覆盖 repo_id"),
    ],
    ids=["only-old-id-algorithm", "only-upsert-covers-id"],
)
def test_each_half_of_u9_alone_keeps_stability_green(
    patched_source: Path, patches: tuple[tuple[str, str, str], ...], label: str
) -> None:
    """单独注入 U9 的任一半时，验收 2/3 的用例**必须仍然绿**——D1 由两道锁共同保证。

    | 注入状态 | 验收 2/3 | 原因 |
    |---|---|---|
    | (a)+(b)（= S3 真实代码） | **红** | U9 复现，见 `COMPOSITE_INJECTIONS` |
    | 只有 (a) | 绿 | 规则 1 不写 `repo_id` |
    | 只有 (b) | 绿 | 同一路径算出的 id 本来就相同 |

    红了说明"另一半"不再独立兜住 D1，先看 `COMPOSITE_INJECTIONS` 里的条目。
    """
    pristine = _apply_patches(patched_source, patches)
    try:
        for test_node in (
            _target_test("test_rebinding_after_origin_added_updates_the_row"),
            _target_test("test_unbound_legacy_id_is_still_reachable_after_origin_change"),
        ):
            result = _run_target_test(patched_source, test_node)
            assert result.returncode == 0, (
                f"{label} 单独存在就把 {test_node} 弄红了——D1 的两道锁不再各自独立。\n"
                f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-1000:]}"
            )
    finally:
        _restore_patches(patched_source, pristine)


def test_repository_source_is_never_modified(patched_source: Path) -> None:
    """所有反向验证都只改副本；跑完之后**仓库里的 `src/` 必须逐字节没变**。

    这条是这套机制的安全锁：注入逻辑一旦写错（例如忘了用副本），它会当场抓住。
    """
    before = _snapshot(SOURCE_ROOT)
    assert _snapshot(patched_source) == before, "副本与仓库源码不一致（复原有问题）"
    assert _snapshot(patched_source)  # 非空，别让一个空 dict 蒙过去
