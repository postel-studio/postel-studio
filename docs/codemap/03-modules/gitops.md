# 03 · gitops — `src/postel_studio/gitops/`

> 层 L0（**起子进程、碰磁盘**）· 203 行 · 上游：[模块索引](README.md) · 相关：[05-security §3.3](../../design/05-security.md) · [06-pitfalls T25](../06-pitfalls.md)

## 职责

**唯一起 `git` 子进程的地方。** 只读探测：这是不是仓库 / 根在哪 / origin 是什么 / 默认分支是谁。

- **不做写操作**（不 commit / 不 checkout / 不 fetch）：S3 的禁止事项，也是 05 §2「判定在执行之前」的前提。
- **不接受引用名参数**（分支 / 提交）：`repo.branches` / `repo.diff` 是 S4。
- **不判断"用户能不能绑这个目录"**：那是 `api/repository.py:_bindable_root()` 的事，见下。

## 文件

| 文件 | 行数 | 内容 |
|---|---|---|
| `__init__.py` | 5 | 包标记（三条硬约束的一句话摘要） |
| `repo.py` | 197 | 子进程外壳 + 四个探测函数 |

## 符号表（`gitops/repo.py`）

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `GIT_TIMEOUT_SECONDS` | 31 | `5.0`：**每个** git 调用的上限（D5） | `run_git`、测试 |
| `_DROPPED_ENV` | 33 | 不继承的 `GIT_DIR` / `GIT_WORK_TREE` 等 | `_environment` |
| `GitUnavailableError` | 43 | git 不存在 / 起不来 → 帧层 `ADAPTER_UNAVAILABLE` | `api/repository.py` |
| `GitTimeoutError` | 47 | 超时 → 帧层 `INTERNAL`（D6 裁决，S19 再细化） | 同上 |
| `_environment()` | 51 | 子进程环境：`GIT_TERMINAL_PROMPT=0` + `GIT_CONFIG_NOSYSTEM=1` + `GIT_OPTIONAL_LOCKS=0` | `run_git`、测试 |
| `git_executable()` | 61 | `shutil.which("git")`，找不到抛 `GitUnavailableError` | `run_git` |
| `run_git(args, *, root)` | 73 | **字面数组 + `shell=False` + timeout** 跑一条只读命令 | 全部探测函数 |
| `_stdout(result)` | 101 | 成功取去空白的 stdout，否则 `None` | 全部探测函数 |
| `repo_root(path)` | 108 | `git rev-parse --show-toplevel`（会 discover） | `_bindable_root` 之外的用途小心，见下 |
| `bare_root(path)` | 123 | 自己就是裸仓库根时返回它 | `_bindable_root` |
| `is_worktree_root(path)` | 131 | `path/.git` 存在（**不碰 git**，filesystem 检查） | `_bindable_root`、测试 |
| `is_repo(path)` | 139 | `repo_root or bare_root`（**同上，别单独当判据**） | 暂无生产调用方；给将来的 doctor |
| `origin_url(root)` | 149 | `git config --get remote.origin.url` → `str \| None` | `api/repository.py:bind` |
| `default_branch(root)` | 163 | `git symbolic-ref --short HEAD` → `str \| None` | 同上 |
| `GIT_ERRORS` | 182 | 两个异常类的元组（"能抛什么"的显式清单） | 文档用 |

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
| 字面数组 + `shell=False` | 05 §3.3 禁止 `shell=True` / `os.system` / 字符串拼接。路径里带 `$(...)` 也伤害不到我们 | `test_bind.py::test_bind_uses_git_in_a_way_that_cannot_prompt`（读环境）+ code review |
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

### `origin_url` 用 `git config --get` 而不是 `git remote get-url`

后者会把 `insteadOf` 重写后的地址吐出来；06 §2.1 要展示的是**原始值**。
两者在无 origin 时退出码都是 1，都按"没有"处理 → `None`（不拿别的 remote 顶替）。

### `default_branch` 用 `symbolic-ref` 而不是 `branch --show-current`

后者依赖"分支列表"的概念，而本刀**明确不做分支列表**（S4）。实测四种情形：

| 仓库 | `symbolic-ref --short HEAD` | 返回 |
|---|---|---|
| 正常仓库 | `main` | `"main"` |
| 刚 `git init`、**无任何提交** | `main`（git 已写 symbolic-ref） | `"main"`（D2 不要求有提交） |
| `git init --bare` | `main` | `"main"` |
| HEAD 游离（检出某个提交） | 失败（rc=1） | `None` —— 不瞎猜 `main` |

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
| 加一个新的 git 探测 | 用 `run_git`；**不要**自己 `subprocess.run`。要 `test_bind.py` 里有一条真实仓库用例 |
| 改 `_environment()` | 05 §3.3 / S3 D5；`test_bind_uses_git_in_a_way_that_cannot_prompt` 会红 |
| 把 `shell=` 改成 `True` 或拼接命令字符串 | **禁止**，05 §3.3 |
| 让 `root` 变成可选（缺省 cwd） | **禁止**：等于放弃 D4 的允许根 |
| 在本层 import `api/` | **禁止**：L0 不许向上 import（[02-layers §1](../02-layers.md#1-五层自下而上)） |
| 加 git **写**操作 | 本刀明确不做；S4+ 要走权限判定（05 §2.1） |

## 测试

全部在 `tests/test_bind.py`（**真实 git 仓库，不 mock**）：

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