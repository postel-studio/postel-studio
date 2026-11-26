# 03 · gitops — `src/postel_studio/gitops/`

> 层 L0（**起子进程、碰磁盘**）· 286 行 · 上游：[模块索引](README.md) · 相关：[05-security §3.3](../../design/05-security.md) · [06-pitfalls T25](../06-pitfalls.md) / [T33](../06-pitfalls.md)

## 职责

**唯一起 `git` 子进程的地方。** 只读探测：这是不是仓库 / 根在哪 / origin 是什么 / 默认分支是谁 /
本地分支有哪些（S4）。

- **不做写操作**（不 commit / 不 checkout / 不 fetch）：S3 的禁止事项，也是 05 §2「判定在执行之前」的前提。
- **不接受引用名参数**（分支 / 提交）：S4 的 `list_branches` 是"枚举全部本地分支"的整体询问，
  没有 ref 参数（`repo.branches` 只收 `repoId`，不切分支）；`repo.diff` 的 `base/head` 属 S5。
- **不判断"用户能不能绑这个目录"**：那是 `api/repository.py:_bindable_root()` 的事，见下。

## 文件

| 文件 | 行数 | 内容 |
|---|---|---|
| `__init__.py` | 6 | 包标记（三条硬约束的一句话摘要 + S4 的 `list_branches`） |
| `repo.py` | 280 | 子进程外壳 + 五个探测函数（含 S4 的分支列表） |

## 符号表（`gitops/repo.py`）

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `GIT_TIMEOUT_SECONDS` | 35 | `5.0`：**每个** git 调用的上限（D5） | `run_git`、测试 |
| `BRANCH_FORMAT` | 40 | `for-each-ref` 的 `--format`：`%00` 分隔四字段（S4 D3） | `list_branches` |
| `_DROPPED_ENV` | 42 | 不继承的 `GIT_DIR` / `GIT_WORK_TREE` 等 | `_environment` |
| `GitUnavailableError` | 52 | git 不存在 / 起不来 → 帧层 `ADAPTER_UNAVAILABLE` | `api/repository.py` |
| `GitTimeoutError` | 56 | 超时 → 帧层 `INTERNAL`（D6 裁决，S19 再细化） | 同上 |
| `BranchInfo` | 61 | frozen dataclass：`name` / `sha`（完整 40 位）/ `upstream` / `is_current`（S4 D1/D4） | `list_branches`、`api/repository.py` |
| `_environment()` | 78 | 子进程环境：`GIT_TERMINAL_PROMPT=0` + `GIT_CONFIG_NOSYSTEM=1` + `GIT_OPTIONAL_LOCKS=0` | `run_git`、测试 |
| `git_executable()` | 88 | `shutil.which("git")`，找不到抛 `GitUnavailableError` | `run_git` |
| `run_git(args, *, root)` | 100 | **字面数组 + `shell=False` + timeout** 跑一条只读命令 | 全部探测函数 |
| `_stdout(result)` | 128 | 成功取**整段去空白**的 stdout，否则 `None`（T33 的陷阱源头） | 全部探测函数 |
| `repo_root(path)` | 135 | `git rev-parse --show-toplevel`（会 discover） | `_bindable_root` 之外的用途小心，见下 |
| `bare_root(path)` | 150 | 自己就是裸仓库根时返回它 | `_bindable_root` |
| `is_worktree_root(path)` | 158 | `path/.git` 存在（**不碰 git**，filesystem 检查） | `_bindable_root`、测试 |
| `is_repo(path)` | 166 | `repo_root or bare_root`（**同上，别单独当判据**） | 暂无生产调用方；给将来的 doctor |
| `origin_url(root)` | 176 | `git config --get remote.origin.url` → `str \| None` | `api/repository.py:bind` |
| `default_branch(root)` | 190 | `git symbolic-ref --short HEAD` → `str \| None` | 同上 |
| `list_branches(root)` | 209 | `for-each-ref refs/heads/` → `list[BranchInfo] \| None`（S4 D3；三态见下） | `api/repository.py:branches` |
| `GIT_ERRORS` | 262 | 两个异常类的元组（"能抛什么"的显式清单） | 文档用 |

## 关键决策与理由

### 五条硬约束（05 §3.3 / S3 D5），改这个文件时是红线

```python
# gitops/repo.py:83-92 —— 四项：字面数组 / 独立路径参数 / 无 shell / 超时
command = [git_executable(), "-C", str(root), *args]
subprocess.run(
    command,
    shell=False,
    capture_output=True,
    text=True,
    timeout=GIT_TIMEOUT_SECONDS,
    env=_environment(),
    stdin=subprocess.DEVNULL,
    check=False,
)
```

| 约束 | 为什么 | 测试守在哪 |
|---|---|---|
| 字面数组 + `shell=False` | 05 §3.3 禁止 `shell=True` / `os.system` / 字符串拼接。路径里带 `$(...)` 也伤害不到我们 | `test_bind.py::test_git_subprocess_shell_is_disabled`（AST 守卫）+ `test_bind_uses_git_in_a_way_that_cannot_prompt`（读环境） |
| `GIT_TERMINAL_PROMPT=0` | git 缺省会向 tty 要密码；凭证助手在无 tty 时可能**挂住**，而这里跑在 WS 处理链路上 | 同上 |
| `timeout=5` | 坏仓库 / 锁文件 / 网络文件系统会让 git 卡住 → 整个网关无响应 | `GIT_TIMEOUT_SECONDS > 0` 断言 |
| `GIT_CONFIG_NOSYSTEM=1` | 用户机器的系统级配置不该影响探测结果 | 同上 |
| 绝对路径 `git` | "git 不存在"必须能被可靠测出来（`PATH=""`）；也避免 `PATH` 里的影子命令 | `test_missing_git_is_adapter_unavailable` |

**`GIT_OPTIONAL_LOCKS=0`** 是加的第四样：只读探测不需要可选锁，避免并发 `git gc` 时
`index.lock` 失败被误判成"仓库有问题"。

### `root` 是显式参数（D4 的落点）

D4 说"绑定之后的所有操作必须以已绑定的 path 为允许根"。落地方式就是：**这个函数只接受一个
根**，S4 的分支列表复用 `run_git(root=<已绑定 path>)` 就天然无法越界。不要在别处再写一个
`subprocess.run(["git", ...])`。

### `repo_root` / `is_repo` **不要**单独当"能不能绑"的判据（T25）

`git -C <任意目录> rev-parse --show-toplevel` 会**向父目录 discover**：

```bash
$ mkdir -p /tmp/ps/outer && git -C /tmp/ps/outer init -q
$ mkdir -p /tmp/ps/plain && git -C /tmp/ps/plain rev-parse --show-toplevel
/tmp/ps            # ← 只要上层某处有 .git，"普通目录"也会被答"是"
```

实测踩到：`tmp_path` 下的普通目录、`/tmp` 本身都被"绑定成功"了（06-pitfalls T25）。
所以本层的探测函数只回答"git 怎么说"，**总判定**在 `api/repository.py:_bindable_root()`：
目标自己带 `.git`、或自己是裸仓库、或它在某个工作区目录**里面**（逐级向上找最近的 `.git`）。

### ⚠️ `origin_url` / `default_branch` 对 **worktree** 会答"链接工作区自己的事"（S3 未解决）

```bash
$ git worktree add ../wt          # 链接工作区；wt/.git 是**文件**（gitdir: …）
$ git -C ../wt config --get remote.origin.url      # 无输出 / rc=1
$ git -C ../wt symbolic-ref --short HEAD
wt                             # ← 链接工作区的分支名
```

两处偏差都是**实测**的（不是推测），S3 保留、未修：

| 情形 | 现状 | 期望（未做） |
|---|---|---|
| worktree 里的 origin | `origin_url()` 得 `None` → **id 退化成路径哈希** | 读 `commondir`（`<wt>/.git` 里的路径）指向的主仓库 config；或者绑 worktree 时直接拒绝 |
| worktree 的默认分支 | 答链接工作区**自己的分支**（`wt`），不是主仓库的 `main` | 主仓库 HEAD，或干脆不展示 |

**为什么本刀不修**：S7 才引入 worktree 概念（04-task-breakdown）。现在"能绑 worktree"
是 D2 字面语义的顺带结果，S3 的验收里没有它，也没有任何下游在用它。已记进
[06 §已知未决 U13](../06-pitfalls.md#已知未决问题)。

**test 的口径**：`test_worktree_root_with_git_file_still_binds` 只断言"能绑、落库的是
worktree 根、id 是 12 位、branch 是字符串"——**不**断言 `main`，因为那正是上面这条已知偏差。
别为了让测试好看去"修" `default_branch` 的语义（那要连 S4 一起想）。
如果 S7 决定**不支持** worktree，正确的做法是让 `_bindable_root` 拒绝 `.git` 是文件的根，
而不是在这里猜。

### `origin_url` 用 `git config --get` 而不是 `git remote get-url`

后者会把 `insteadOf` 重写后的地址吐出来；06 §2.1 要展示的是**原始值**。
两者在无 origin 时退出码都是 1，都按"没有"处理 → `None`（不拿别的 remote 顶替）。

### `default_branch` 用 `symbolic-ref` 而不是 `branch --show-current`

后者依赖"分支列表"的概念（S3 时本刀不做分支列表；S4 的 `list_branches` 只用它枚举
`refs/heads/`，仍不用 `branch` 的人类可读输出）。实测四种情形：

| 仓库 | `symbolic-ref --short HEAD` | 返回 |
|---|---|---|
| 正常仓库 | `main` | `"main"` |
| 刚 `git init`、**无任何提交** | `main`（git 已写 symbolic-ref） | `"main"`（D2 不要求有提交） |
| `git init --bare` | `main` | `"main"` |
| HEAD 游离（检出某个提交） | 失败（rc=1） | `None` —— 不瞎猜 `main` |

### S4：`list_branches` 一次调用取全字段，三态**不许合并**

命令与格式固定（S4 D2/D3）：

```bash
git -C <root> for-each-ref refs/heads/ \
    --format=%(refname:lstrip=2)%00%(objectname)%00%(upstream:short)%00%(HEAD)
```

| 决策 | 理由 |
|---|---|
| 只列 `refs/heads/` | 不带它时 `refs/remotes/origin/main` 也会出现，"共几个分支"与"当前分支"同时失真（D2，实测） |
| 一次调用同时拿"列表"与"当前" | 两次调用之间用户可能正在切分支，"列表"与"当前"就会各说各话（D3） |
| 不解析 `git branch` | 颜色 / 语言 / 终端宽度都会改变人类可读输出（S4 禁止事项，D7 第 7 行回归） |
| `%00` 作字段分隔符 | 分支名不许含 NUL，天然安全；空格作分隔会被分支名/upstream 里的字符干扰 |
| `sha` 存完整 40 位 | 截 8 位是**帧层边界格式化**（`api/repository.py`），两层各自可测（D4） |

三态（D4/D5 的基础）：`list`（可能空，空仓库 = 成功）/ `None`（git 非零退出 = 不是仓库或路径没了）/
抛 `GitUnavailableError`·`GitTimeoutError`（起不来 / 卡住）。**任何两个都不许合并**。

#### T33：`%(HEAD)` 的"空格"与 `_stdout()` 的整段 strip（详见 [06 T33](../06-pitfalls.md)）

`%(HEAD)` 对**非当前**分支输出的是**一个空格**，不是空串；而 `_stdout()` 对**整段** stdout
`.strip()`，于是**最后一条记录末尾的空格被吃掉**：

```text
# 最后一条记录有 upstream 且非当前时，整段 strip 后只剩 3 个字段
'zz\x00<sha>\x00origin/zz'          # marker 字段被吞掉
```

解析必须：① 当前判定写 `marker.strip() == "*"`，**不能** `if not marker`（除最后一行外全判反）；
② 缺第 4 字段时补空（= 非当前），**不要**当成格式错误整体返回 `None`；③ 字段各自 `.strip()`。
回归测试：`tests/test_branches.py` 的 D7 第 7 行（`color.ui always` + 非 ASCII 名）与第 3 行
（最后一条记录带 upstream——正是 3 字段那条路径）。

### 被推翻的方案

| 方案 | 为什么推翻 |
|---|---|
| 直接 `subprocess.run(["git", ...])`（裸命令名） | `PATH` 被清空时 `FileNotFoundError` 才能被发现；但裸名会在 `PATH` 里有影子 git 时静默跑错东西。改 `which` + 绝对路径 |
| 用 `git rev-parse --show-toplevel` 当 `is_repo` 的判据 | git 会 discover 到父目录 → 普通目录被判成仓库（T25）。保留函数但**不作为判据** |
| 用 `git status --porcelain` 判 `isDirty` | 07 §4.1 示例里有 `isDirty`，但 S3 不需要，且它和"是不是仓库"无关。**不填假字段**，留给 S4 |
| 把超时设成 30 秒 | D5 建议 5 秒；本地只读命令毫秒级，30 秒等于"卡住时整个网关被占 30 秒" |

## 修改须知

| 如果你要… | 还波及 |
|---|---|
| 加一个新的 git 探测 | 用 `run_git`；**不要**自己 `subprocess.run`。要 `test_bind.py` / `test_branches.py` 里有一条真实仓库用例 |
| 改 `BRANCH_FORMAT` 或它的解析 | T33（`%(HEAD)` 空格 + 整段 strip）、D7 第 1/2/3/5/7 行；`test_branches.py` 全部解析用例 |
| 改 `_environment()` | 05 §3.3 / S3 D5；`test_bind_uses_git_in_a_way_that_cannot_prompt` 会红 |
| 把 `shell=` 改成 `True` 或拼接命令字符串 | **禁止**，05 §3.3 |
| 让 `root` 变成可选（缺省 cwd） | **禁止**：等于放弃 D4 的允许根 |
| 在本层 import `api/` | **禁止**：L0 不许向上 import（[02-layers §1](../02-layers.md#1-五层自下而上)） |
| 加 git **写**操作 | 本刀明确不做；S4+ 要走权限判定（05 §2.1） |

## 测试

S3 的探测在 `tests/test_bind.py`，S4 的分支列表在 `tests/test_branches.py`（**真实 git 仓库，不 mock**）：

| 测试 | 守什么 |
|---|---|
| `test_bind_regular_repo_returns_hex12_id_name_and_main` | 基本探测（root / branch / origin=None） |
| `test_bind_subdirectory_stores_repo_root_not_the_subdirectory` | D2 子目录校正 |
| `test_bind_bare_repo_succeeds_and_default_branch_may_be_null` | 裸仓库 |
| `test_bare_repo_without_any_head_branch_reports_null` | HEAD 探测不到 → `None` |
| `test_bind_repo_without_any_commit_succeeds` | 无提交的仓库 |
| `test_missing_git_is_adapter_unavailable` | `PATH=""` → `ADAPTER_UNAVAILABLE` |
| `test_bind_uses_git_in_a_way_that_cannot_prompt` | `GIT_TERMINAL_PROMPT` / `GIT_CONFIG_NOSYSTEM` / timeout |
| `test_root_level_plain_directory_that_contains_a_repo_is_not_a_repo` | T25 回归锁 |
| `test_bind_writes_nothing_into_the_user_repository` | 只读（`git status --porcelain` 前后一致） |
| `test_git_subprocess_shell_is_disabled` | AST 静态守卫：命令是字面数组、`shell=False`、有 timeout、全项目只有这里起子进程 |
| `test_kernel_subprocess_ignores_pythonpath_like_gateway_util_does` | 端到端那条验的是**安装版**内核（不是被注入的副本） |

> **机制的守护者**：`tests/test_reverse_checks.py` 把上面若干条**注入缺陷后必须红**钉成可执行
> 用例（`GIT_TERMINAL_PROMPT` / `GIT_CONFIG_NOSYSTEM` / 超时 / shell / 子目录校正 / `.git` 判定 …）。
> 加一条新约束时，顺手在里面加一行注入——那是最省事的"这条测试真的守得住"的证明。

`tests/test_branches.py` 里与 gitops 层直接相关的用例：

| 测试 | 守什么 |
|---|---|
| `test_branches_returns_names_in_refname_order_with_short_shas` | D7 第 1 行 / D1 / D4：名称集合、字典序、gitops 层 `sha` 是完整 40 位（帧层截 8 位在 `api/repository.py`） |
| `test_branch_current_flag_is_only_on_the_head_branch` | D7 第 2 行 / D1：只有 HEAD 指向的那条 `is_current=True` |
| `test_branch_upstream_is_short_name_and_none_when_missing` | D7 第 3 行 / D1：`upstream` 有则短名、无则 `None`（不是 `""`）；含"最后一条记录带 upstream"的 3 字段路径 |
| `test_empty_repo_returns_empty_list_as_success` | D7 第 4 行 / D4：空列表是成功 |
| `test_detached_head_still_lists_all_branches_with_none_current` | D7 第 5 行 / D1 |
| `test_only_local_branches_are_listed_after_push` | D7 第 6 行 / D2 |
| `test_parsing_does_not_depend_on_human_readable_output` | D7 第 7 行 / T33：`color.ui always` + 非 ASCII 名（`功能/中文`） |
| `test_list_branches_returns_none_for_non_repo_and_missing_path` | D7 第 10 行 / D4：三态里的 `None` |
