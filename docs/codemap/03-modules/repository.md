# 03 · repository — `src/postel_studio/api/repository.py`

> 层 L3 · 378 行 · 上游：[模块索引](README.md) · 相关：[gitops](gitops.md) · [storage-db](storage-db.md) · [domain](domain.md) · 接口：[07 §2.2/§4.1/§4.5/§4.6](../../design/07-api-contract.md)

## 职责

`repo.bind` / `repo.list` / `repo.branches` / `repo.diff` 的**业务编排**：路径 → 校验 → git
探测 → 落库 → 帧载荷（S4 的 `branches` 与 S5 的 `diff` 是只读的：`repoId` → 已绑定的 path
当允许根 → git → 帧载荷；两者都不落库、不缓存）。

**这一层不碰 fastapi**（S3 涉及文件表的要求）。它只接收 `dict` 参数、返回 `dict`，
把下游异常翻成 07 §1.3 的**稳定错误码**。怎么接上 WebSocket 是 `api/gateway.py` 的事。

## 符号表

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `NOT_A_REPO` / `BAD_CWD` / `ADAPTER_UNAVAILABLE` / `REPO_NOT_BOUND` / `BAD_REF` / `NOT_SUPPORTED` | 39-45 | 07 §1.3 的码（**不许新造**；`BAD_REF`/`NOT_SUPPORTED` 是 S5 第一次用） | `bind` / `get` / `branches` / `diff` |
| `MSG_*`（14 个） | 48-63 | 错误文案常量，**没有一个字来自用户输入** | `_fail` 调用点 |
| `DEFAULT_MAX_CHARS` / `MIN_MAX_CHARS` / `MAX_MAX_CHARS` | 67-69 | 截断契约值：100k / 1000 / 200k（S5 D6，**不许改**） | `_require_max_chars`、测试 |
| ~~`MAX_REF_LENGTH`~~ | — | **S7 搬到 `domain/models.REF_NAME_MAX_LENGTH`**（同层模块不互相 import，见 02-layers §3） | `_require_ref` |
| `BranchPayloads` | 75 | `list[dict[str, object]]` 的模块级别名（类里有方法叫 `list`，类体里直接写 `list[…]` 注解会被 mypy 判 `valid-type`） | `branches` 返回注解 |
| `_fail(code, message)` | 76 | 造 `frames.FrameError`（网关原样转发） | 内部 |
| `_require_path(params)` | 81 | `path` 必须是非空字符串，否则 `INVALID_PARAMS` | `bind` |
| ~~`ref_name_is_valid`~~ | — | **S7 搬到 `domain/models.ref_name_ok`**：`repo.diff` 的 base/head 与 `worktree.acquire` 的 baseRef 共用一份，而 L3 之间不互相 import | `_require_ref`、`api/worktrees.py` |
| `_require_ref(params, field)` | 89 | 入站 ref 的形状校验（S5 D3.1 六条；lone surrogate 用 `encode` 才抓得到） | `diff` |
| `_require_max_chars(params)` | 108 | `maxChars` 缺省/区间/类型（先排 `bool`，S5 D6） | `diff` |
| `_inside_git_dir(root, target)` | 123 | **T29/T31：`root` → `target` 的相对路径里有 `.git` 段 → 不是仓库入口** | `_bindable_root` |
| `_bindable_root(target)` | 144 | **D2 子目录校正的总判定**（见下） | `bind` |
| `_split(repo)` | 180 | `Repo` → 帧载荷（`camelCase`，07 §4.2） | `list` / `bind` / `get` |
| ~~`repo_path_is_gone`~~ | — | **S7 搬到 `gitops/repo.is_usable_repo(path)`**：它只问 git 语义、不认识 `Repo`；`diff` 与 `api/worktrees.py` 都调它（同一族 T25 陷阱） | `diff`、`api/worktrees.py` |
| `RepositoryService` | 195 | 业务对象（构造时注入 `RepoStore`） | `app.create_app` |
| `RepositoryService.list()` | 201 | `repo.list`：返回 `repo[]` | `gateway.Providers.repo_list` |
| `RepositoryService.bind(params)` | 205 | `repo.bind`：探测 + 落库 + 返回落库后的记录 | `gateway.Providers.repo_bind` |
| `RepositoryService._require_repo(params)` | 250 | `repoId` 校验 + 查库 → `Repo`（`INVALID_PARAMS` / `REPO_NOT_BOUND` 的落点） | `get` / `branches` / `diff` |
| `RepositoryService.get(params)` | 265 | 按 `repoId` 取一条（`get` 本身暂无帧方法调用） | 暂无生产调用方 |
| `RepositoryService.branches(params)` | 269 | `repo.branches`：`repoId` → 允许根 → `gitops.list_branches` → `branch[]`（S4 D1/D5） | `gateway.Providers.repo_branches` |
| `RepositoryService.diff(params)` | 361 | `repo.diff`：形状校验 → 路径存活 → 解析两个 ref → stat/patch → 截断 + camelCase 载荷（S5 D1–D8） | `gateway.Providers.repo_diff` |

## 关键决策与理由

### `_bindable_root`：D2 的「子目录校正」，以及它踩过的坑

判定顺序（**别改顺序**，也别改回"git 说有输出就算仓库"）：

```
0. target 自身或任何祖先的名字是 ".git"        → None（T29：.git 内部结构不是仓库入口）
1. target 是目录 且 target/.git 存在            → 绑 target
2. target 是目录 且 git 说 is-bare-repository   → 绑 target（裸仓库，D2 接受）
3. target 不是目录（文件 / 不存在）             → None
4. 从 target 逐级向上，第一个带 .git 的祖先     → 绑它（子目录校正 proj/src → proj）
5. 都没有                                        → None
```

**第 0 条必须排在前面**：`.git/objects` 的祖先链里就有 `.git`，第 4 条会把它校成外层仓库
（S3 复审退回的缺陷，见 [06 T29](../06-pitfalls.md#t29--粘-git-内部路径会被静默改绑到外层仓库审核发现已修)）。
判据是**名字**而不是"`.git` 存在"——worktree 根里 `.git` 是**文件**，它是合法根，不能被误杀。

- 第 1、4 步只看 `path/.git` 是否存在（**文件系统检查，不依赖 git**）。worktree 里 `.git`
  是文件，`exists()` 同样为真。
- 第 2 步才问 git（裸仓库没有 `.git`，只能问 `rev-parse --is-bare-repository`）。
- **为什么不直接用 `git rev-parse --show-toplevel` 判"是不是仓库"**：它会向父目录 discover。
  实测：`tmp_path` 下的普通目录、`/tmp` 本身都会被答"是"，于是"绑定成功"了（T25）。
- **接受的近似**：第 4 步无法区分 `proj/src`（真子目录）与 `proj/某个普通目录`——两者在路径
  上等价。按 D2 的字面语义一律校到 `proj`。见 [06 §已知未决 U11](../06-pitfalls.md#已知未决问题)。

### S5：`diff` 的顺序就是契约（D7）、格式化在边界（D8）、截断不夹紧（D6）

```python
# api/repository.py:346 —— 顺序：参数形状 → 资源存在 → ref → 语义
repo = self._require_repo(params)  # #1 INVALID_PARAMS / #2 REPO_NOT_BOUND
base = _require_ref(params, "base")  # #3 INVALID_PARAMS（六条形状）
head = _require_ref(params, "head")
max_chars = _require_max_chars(params)  # D6：越界/类型错 → INVALID_PARAMS（不夹紧）
if _repo_path_is_gone(repo):  # #4 REPO_NOT_BOUND：**先于** ref 解析
    raise _fail(REPO_NOT_BOUND, MSG_REPO_PATH_MISSING)
base_sha = gitops.resolve_commit(...)  # #5 BAD_REF：不存在 / 不是 commit
head_sha = gitops.resolve_commit(...)
stat = gitops.diff_stat(...)  # #6 BAD_REF：三点没有共同祖先
patch = gitops.diff_patch(...)
return {
    "stat": {"filesChanged": ..., "insertions": ..., "deletions": ...},
    "patch": patch[:max_chars],
    "truncated": len(patch) > max_chars,
}
```

| 裁决 | 为什么 |
|---|---|
| **#4 必须先判** | 路径被删时 `rev-parse` 同样失败；不先探测就会把「仓库没了」报成「ref 不存在」——用户会去改 ref，但真正的问题是仓库被删 / 搬走（D7 #4） |
| 截断与 `camelCase` 只在这一层 | L0 给完整、snake_case 的事实（`DiffStat`）；截断（按**字符**切，允许切在行中间）与字段名转换是边界格式化，两层各自可测（D8） |
| `stat` 不受截断影响 | 统计描述**完整**差异；让它跟着 `maxChars` 变形等于撒谎（D6） |
| 空差异是**成功** | `stat 0/0/0` + `patch ""` + `truncated false`——base 与 head 同一 commit 不是错误（D6） |
| `maxChars` 越界**报错**不夹紧 | 夹紧会让调用方以为拿到了它要的长度（D6） |
| `max_chars` 类型先排除 `bool` | `isinstance(True, int)` 为真，`maxChars: true` 会被当成 1（D6 点名的边界） |
| patch 在 stat 之后 | 两者用**同一对已解析的 object id**，描述同一对 commit；stat 成功而 patch 失败只能是"问不出来"（`BAD_REF`） |

错误码（**一个都不新增**，07 §1.3 之内）：`INVALID_PARAMS`（形状）/ `REPO_NOT_BOUND`（repoId
或路径没了）/ `BAD_REF`（解析不到、无共同祖先）/ `ADAPTER_UNAVAILABLE` / `INTERNAL`（超时）/
`NOT_SUPPORTED`（非 UTF-8 ref 名）。完整表见 [07 §4.6](../../design/07-api-contract.md)。

### S4：`branches` 的三态与错误码映射（D4/D5）

```python
# api/repository.py:214 —— 允许根只有一处来源：库里那条记录的 path
repo = self._require_repo(params)  # INVALID_PARAMS / REPO_NOT_BOUND
found = gitops.list_branches(Path(repo.path))
if found is None:  # git 非零退出 = 不是仓库 / 路径没了
    raise _fail(REPO_NOT_BOUND, MSG_REPO_PATH_MISSING)  # 「仓库路径已不存在」
return [
    {"name": b.name, "sha": b.sha[:8], "isCurrent": b.is_current, "upstream": b.upstream}
    for b in found
]
```

| gitops 返回 | 帧层结果 | 为什么 |
|---|---|---|
| `[]`（空仓库 0 提交） | **成功** `data: []` | D5：空仓库是正常状态，不是错误 |
| `None`（非零退出） | `REPO_NOT_BOUND` + 「仓库路径已不存在」 | D5：用户删了仓库是**正常处境**；`INTERNAL` 会报成"未预期错误需报障" |
| `GitUnavailableError` | `ADAPTER_UNAVAILABLE` | 同 S3 D5 |
| `GitTimeoutError` | `INTERNAL` | 同 S3 D6（S19 的 doctor 再细化） |

- **截 8 位发生在这一层**（D4 的边界纪律）：`gitops.BranchInfo.sha` 是完整 40 位，帧载荷是
  8 位小写 hex。两层各自可测，别把截断挪到 gitops 或前端。
- `upstream` 是 `None` 时**原样**放进载荷（`None`），不写成 `""`——D1 把它定为 `null`。
- `branches` **不落库、不缓存**（06-data-model §8）：每次按需问 git，所以"用户切了分支"立刻可见。
- `_require_repo` 是唯一允许的既有代码重构（S4 实施要求）：`get()` 与 `branches()` 共用，
  "允许根 = 已绑定 path"只有一个实现。

### 错误码只用 07 §1.3 的六个，一个都不新造（D6）

| 场景 | 码 | 在哪判 |
|---|---|---|
| `path` 缺失 / 不是字符串 / 空串 | `INVALID_PARAMS` | `_require_path` |
| `path` 不是绝对路径（含 `~` 开头） | `BAD_CWD` | `bind`（`Path(raw).is_absolute()`） |
| 不存在 / 是文件 / 不是仓库 | `NOT_A_REPO` | `_bindable_root` 返回 `None` |
| `git` 不在 `PATH` / 起不来 | `ADAPTER_UNAVAILABLE` | `except GitUnavailableError` |
| `git` 超时 | `INTERNAL` | `except GitTimeoutError`（**没有** `GIT_TIMEOUT`，D6 明确不造） |
| 输出含非 UTF-8 的 ref 名（D3.2 / U15） | `NOT_SUPPORTED` | `except GitOutputUndecodableError` |
| `repoId` 不存在 | `REPO_NOT_BOUND` | `get` |

### 05 §4.2：错误 message 不回显用户输入

**所有 `MSG_*` 都是常量**，没有一处 `f"{path}"`。这是最容易顺手写错的地方：
`f"路径不是 git 仓库：{target}"` 看起来更有用，但它把解析后的绝对路径送进了帧与日志。

```python
# ✅ 仓库里的写法（api/repository.py:41）
MSG_NOT_A_REPO: Final = "路径不是 git 仓库"
# ❌ 看起来更有用，但把解析后的绝对路径送进了帧与日志（05 §4.2）
#    raise _fail(NOT_A_REPO, f"{target} 不是仓库")
```

`test_not_a_repo_message_does_not_echo_any_user_path` 用一段独一无二的目录名片段断言
`message` / `str(exc)` / `repr(exc)` 里都不出现它。

### `isDirty` 不填假值

07 §4.1 的示例响应里有 `isDirty: true`，但 S3 不读工作区状态（那是 S4 读 diff 的前提）。
`_split` **不输出**这个字段，而不是输出 `false`——"不知道"与"干净"是两件事，
填假的 `false` 就是撒谎。客户端按缺省处理即可（07 §5：允许新增字段，客户端忽略未知字段）。

### `bind` 里 `gitops.origin_url` 只问一次

两次调用的答案理论上可能不同（用户恰好在改配置），只问一次让"算 id 用的 origin"与
"落库的 origin"一定是同一个值。

### S5 / U15：非 UTF-8 refname 不再报 INTERNAL

`branches` 与 `bind` 都补了 `except gitops.GitOutputUndecodableError → NOT_SUPPORTED`：
`for-each-ref` / `symbolic-ref` 的输出解不出合法 UTF-8 时回稳定的 `NOT_SUPPORTED` + 一句人话，
而不是「内部错误，请查看终端」。**不要**改成 `errors="replace"` 糊过去（mangled ref 名回传 git
会变 `BAD_REF` 甚至指向别的 ref，见 06 U15 / 本文 gitops 页）。

## 修改须知

| 如果你要… | 还波及 |
|---|---|
| 改 `_bindable_root` | T25 的回归锁 `test_root_level_plain_directory_that_contains_a_repo_is_not_a_repo`、D2 的验收（子目录校正）、`test_plain_directory_inside_another_working_tree_is_treated_as_that_repos_subdirectory` |
| 加错误码 | **禁止**（D6）；确实需要时先改 07 §1.3 表，再回来 |
| 让 `message` 带上路径 | **禁止**（05 §4.2）|
| 改 `repo.branches` | D5 的码表（07 §1.3，**不许新造**）、`gitops.list_branches` 的三态、D7 第 8 行；**不要**接受新的路径/ref 参数（允许根只能是已绑定的 path） |
| 在本层 `import fastapi` | **禁止**（S3 涉及文件表的要求）；帧形状用 `frames` 里的纯函数 |
| 在本层直接 `open()` / `subprocess` | **禁止**：那是 `storage` / `gitops` 的活（02-layers） |

## 测试

`tests/test_bind.py`（真实 git 仓库 + 真 SQLite，**不 mock**）：

| 测试 | 守什么 |
|---|---|
| `test_bind_regular_repo_returns_hex12_id_name_and_main` | 验收 1 的载荷 |
| `test_bind_subdirectory_stores_repo_root_not_the_subdirectory` | D2（**必须**断言） |
| `test_bind_path_with_trailing_slash_and_dotdot_resolves_to_root` / `..._through_symlink_...` | D4 `resolve()` |
| `test_bind_non_repo_directory_is_not_a_repo` / `test_not_a_repo_message_does_not_echo_any_user_path` | D6 + 05 §4.2 |
| `test_bind_git_directory_itself_is_not_a_repo` / `..._inside_the_git_directory_...` / `..._with_a_dot_git_segment` | T29：`.git` 内部结构不是入口 |
| `test_worktree_root_with_git_file_still_binds` / `..._subdirectory_still_corrects_...` | T29 防误伤（`.git` 是文件） |
| `test_bind_relative_path_is_bad_cwd[~]` 等 5 个 | `BAD_CWD`（含 `~` 不展开） |
| `test_bind_nonexistent_path_is_not_a_repo_not_a_crash` / `test_bind_file_path_is_not_a_repo` | 不崩 |
| `test_missing_git_is_adapter_unavailable` | `PATH=""` |
| `test_bind_rejects_missing_or_non_string_path`（4 组） | `INVALID_PARAMS` |
| `test_get_reports_repo_not_bound_for_unknown_id` | `REPO_NOT_BOUND` |
| `test_failed_bind_leaves_no_half_state` | 验收 2「不留半状态」 |
| `test_list_returns_array_payload_with_camel_case_keys` | 07 §4.2 边界转换 |
| `test_websocket_bind_then_list_returns_the_repo` | 端到端 |

### S4 的分支列表（`tests/test_branches.py`）

| 测试 | 守什么 |
|---|---|
| `test_branches_returns_names_in_refname_order_with_short_shas` | D7 第 1 行 / D1：四字段、`sha` 8 位小写 hex、顺序为 refname 字典序 |
| `test_branch_error_codes_for_bad_params`（4 组参数化） | D7 第 8 行 / D5：缺/非字符串/空串 → `INVALID_PARAMS`；未知 `repoId` → `REPO_NOT_BOUND` |
| `test_deleted_repo_path_maps_to_repo_not_bound` / `test_repo_whose_dot_git_was_removed_maps_to_repo_not_bound` | D5：路径没了 / `.git` 被删 → `REPO_NOT_BOUND`（文案「仓库路径已不存在」） |
| `test_empty_repo_returns_empty_list_as_success` | D5：空仓库 `data == []`，不是错误 |
| `test_missing_git_executable_maps_to_adapter_unavailable` | D5：`PATH=""` → `ADAPTER_UNAVAILABLE`（真让 git 找不到，不 mock 解析） |
| `test_git_timeout_maps_to_internal` | D5：超时 → `INTERNAL`（只在边界注入异常，钉住码映射） |
| `test_websocket_branches_roundtrip_matches_service_layer` | D7 第 9 行：真 WS 帧 `result.data` 形状与 `seq`，与直调服务层逐字段一致 |

### S5 的差异视图（`tests/test_diff.py`）

| 测试 | 守什么 |
|---|---|
| `test_stat_exact_numbers` / `test_three_dot_ignores_later_base_commits` | D10 第 1/2 行：stat 三个数字、三点语义 |
| `test_stat_counts_binary_file_without_line_counts` / `test_stat_counts_mode_only_change` / `test_rename_counts_as_delete_plus_add` | D10 第 3/4/5 行 / D4/D5 |
| `test_same_ref_is_empty_diff_not_error` / `test_truncation_boundary_both_sides` | D10 第 6/8 行 / D6 |
| `test_max_chars_rejects_out_of_range_and_bool`（6 组参数化） | D10 第 9 行 / D6：`0` / `999` / `200001` / `"1000"` / `true` / `1.5` |
| `test_bad_refs_are_bad_ref` / `test_ref_starting_with_dash_is_rejected` / `test_ref_with_lone_surrogate_is_invalid_params` | D10 第 10/11 行 / D2/D3.1 |
| `test_patch_has_no_ansi_when_color_ui_always` / `test_patch_ignores_external_diff_driver` / `test_non_utf8_content_is_replaced_not_fatal` | D10 第 12/13/14 行 / D3.3/D4（反向注入 D12 #1/#2 的靶子） |
| `test_deleted_repo_path_is_repo_not_bound` / `test_unrelated_histories_are_bad_ref` | D10 第 15/16 行 / D7 #4、D1 |
| `test_non_ascii_path_is_readable_not_escaped` | D10 第 17 行 / D4（`core.quotePath=false`） |
| `test_ws_repo_diff_matches_service` | D10 第 21 行：真 WS 帧与直调服务层逐字段一致 |
| 页面契约 7 条（`test_page_*`） | D9 的 DOM/颜色/文案/`innerHTML` 零使用（真实 DOM 归 `tools/acceptance/s5_diff.mjs`） |
