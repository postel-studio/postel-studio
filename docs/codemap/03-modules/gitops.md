# 03 · gitops — `src/postel_studio/gitops/`

> 层 L0（**起子进程、碰磁盘**）· 500 + 269 行 · 上游：[模块索引](README.md) · 相关：[05-security §3.3](../../design/05-security.md) · [06-pitfalls T25](../06-pitfalls.md) / [T33](../06-pitfalls.md) / [T34](../06-pitfalls.md) / [T36](../06-pitfalls.md) / [T40](../06-pitfalls.md)

## 职责

**唯一起 `git` 子进程的地方**——分成两个文件，读面与写面各一个：

| 文件 | 面 | 做什么 |
|---|---|---|
| `repo.py` | **只读** | 这是不是仓库 / 根在哪 / origin / 默认分支 / 本地分支（S4）/ 两个 commit 之间的差异（S5） |
| `worktrees.py` | **写**（S7 三条 + S11 三条） | `worktree add` / `remove` / `prune`（S7）+ `commit_all` / `create_branch` / `head_commit`（S11） |

- **`repo.py` 不做写操作**（不 commit / 不 checkout / 不 fetch / 不 worktree）：S3 的禁止事项，也是
  05 §2「判定在执行之前」的前提。**S7 起写操作存在**，但只存在于 `worktrees.py`，而且只有上表那三条。
- **唯一**把用户给的 ref 字符串送进 git 的入口是 `resolve_commit`（S5 D2）：它先解析成
  object id，之后的 `diff_stat` / `diff_patch` / `worktree add` 只用那个 id。`repo.branches` 仍只收
  `repoId`（没有 ref 参数）。
- **不判断"用户能不能绑这个目录"**：那是 `api/repository.py:_bindable_root()` 的事，见下。

## 文件

| 文件 | 行数 | 内容 |
|---|---|---|
| `__init__.py` | 6 | 包标记（三条硬约束的一句话摘要 + S4 的 `list_branches`） |
| `repo.py` | 500 | 子进程外壳 + 八个探测函数（S3 的四个、S4 的分支列表、S5 的 ref 解析 / stat / patch）+ **严格解码**（S5 结 U15）+ `git_env()`（S7 让写面共用同一份环境） |
| `worktrees.py` | 226 | 写面（S7）：`add_worktree` / `remove_worktree` / `prune_worktrees` / `list_worktrees` / `branch_exists` / `is_dirty` + `GitRefusedError` |

## 符号表（`gitops/repo.py`）

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `GIT_TIMEOUT_SECONDS` | 37 | `5.0`：**每个** git 调用的上限（D5） | `run_git`、测试 |
| `BRANCH_FORMAT` | 42 | `for-each-ref` 的 `--format`：`%00` 分隔四字段（S4 D3） | `list_branches` |
| `_DROPPED_ENV` | 71 | 不继承的 `GIT_DIR` / `GIT_WORK_TREE` 等 | `_environment` |
| `DIFF_FLAGS` | 52 | diff 输出的**确定性 flag**：`--no-color` / `--no-ext-diff` / `--no-textconv` / `--no-renames` / `--unified=3`（S5 D4，T34） | `diff_patch` |
| `STAT_FLAGS` | 62 | 同一组减去 `--unified`（`--numstat` 与它同给会连 patch 一起输出，S5 实测，T36） | `diff_stat` |
| `_DIFF_CONFIG` | 66 | `-c core.quotePath=false`（必须在 `diff` **子命令之前**） | `diff_stat` / `diff_patch` |
| `_OBJECT_ID` | 69 | 40/64 位小写 hex 的形状校验（不信 rev-parse 的回显，S5 D2） | `resolve_commit` |
| `GitUnavailableError` | 81 | git 不存在 / 起不来 → 帧层 `ADAPTER_UNAVAILABLE` | `api/repository.py` |
| `GitTimeoutError` | 85 | 超时 → 帧层 `INTERNAL`（D6 裁决，S19 再细化） | 同上 |
| `GitOutputUndecodableError` | 89 | 输出不是合法 UTF-8（strict 解码）→ 帧层 `NOT_SUPPORTED`（**S5 结 U15**，D3.2） | `api/repository.py` |
| `BranchInfo` | 100 | frozen dataclass：`name` / `sha`（完整 40 位）/ `upstream` / `is_current`（S4 D1/D4） | `list_branches`、`api/repository.py` |
| `DiffStat` | 118 | frozen dataclass：`files_changed` / `insertions` / `deletions`（S5 D5；纯事实、snake_case） | `diff_stat`、`api/repository.py` |
| `_environment()` | 131 | 子进程环境：`GIT_TERMINAL_PROMPT=0` + `GIT_CONFIG_NOSYSTEM=1` + `GIT_OPTIONAL_LOCKS=0` | `run_git`、测试 |
| `git_env()` | 141 | `_environment()` 的公开别名：**写面**（`gitops/worktrees.py`）也用它，环境只有一份（S7 D5） | `gitops/worktrees.py` |
| `git_executable()` | 151 | `shutil.which("git")`，找不到抛 `GitUnavailableError` | `run_git` |
| `run_git(args, *, root, decode_errors="strict")` | 163 | **字面数组 + `shell=False` + timeout**；`decode_errors` 原样给 `subprocess.errors`（S5 D3.2） | 全部探测函数 |
| `_stdout(result)` | 206 | 成功取**整段去空白**的 stdout，否则 `None`（T33 的陷阱源头） | 单值询问（`resolve_commit` 也用它校验形状） |
| `repo_root(path)` | 213 | `git rev-parse --show-toplevel`（会 discover） | `_bindable_root` 之外的用途小心，见下 |
| `bare_root(path)` | 248 | 自己就是裸仓库根时返回它 | `_bindable_root`、`is_usable_repo` |
| `is_worktree_root(path)` | 236 | `path/.git` 存在（**不碰 git**，filesystem 检查） | `_bindable_root`、测试 |
| `is_repo(path)` | 244 | `repo_root or bare_root`（**同上，别单独当判据**） | 暂无生产调用方；给将来的 doctor |
| `is_usable_repo(path)` | 274 | 绑定路径**现在还能不能当仓库用**（目录在 + 有 `.git` 或裸仓库）：S5 D7 #4 与 S7 共用的一条，T25 的判据 | `api/repository.py` 的 `diff`、`api/worktrees.py` |
| `origin_url(root)` | 254 | `git config --get remote.origin.url` → `str \| None` | `api/repository.py:bind` |
| `default_branch(root)` | 268 | `git symbolic-ref --short HEAD` → `str \| None` | 同上 |
| `list_branches(root)` | 287 | `for-each-ref refs/heads/` → `list[BranchInfo] \| None`（S4 D3；三态见下） | `api/repository.py:branches` |
| `resolve_commit(root, ref)` | 343 | ref → **commit** object id；`rev-parse --verify --end-of-options <ref>^{commit}` + 形状校验（S5 D2） | `api/repository.py:diff` |
| `_git_output(result)` | 374 | 成功返回**原样** stdout（不 strip；diff 是内容）否则 `None` | `diff_patch` |
| `diff_stat(root, base, head)` | 383 | `git -c core.quotePath=false diff --numstat -z <a>...<b>` → `DiffStat \| None`（S5 D5，T36） | `api/repository.py:diff` |
| `diff_patch(root, base, head)` | 424 | `git … diff <DIFF_FLAGS> <a>...<b>` → 完整 patch → `str \| None`（S5 D1/D4） | 同上 |
| `GIT_ERRORS` | 449 | 三个异常类的元组（"能抛什么"的显式清单） | `api/repository.py`、测试 |

## 关键决策与理由

### 五条硬约束（05 §3.3 / S3 D5），改这个文件时是红线

```python
# gitops/repo.py:178-192 —— 字面数组 / 独立路径参数 / 无 shell / 超时 / 严格解码
command = [git_executable(), "-C", str(root), *args]
subprocess.run(
    command,
    shell=False,
    capture_output=True,
    text=True,
    errors=decode_errors,  # S5 D3.2：缺省 strict（U15 结案）
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

### S5 / U15 结案：`run_git(decode_errors=…)` 与 `GitOutputUndecodableError`

S3/S4 的 `run_git(text=True)` 是严格 UTF-8 解码、但**不捕获** `UnicodeDecodeError`：仓库里有
非 UTF-8 refname 时它一路逃到网关 `except Exception` → `INTERNAL`（06 U15）。按 S5 D3.2 的
裁决改成显式异常：

| `decode_errors` | 用在 | 解码失败时 |
|---|---|---|
| `"strict"`（缺省） | 身份类输出（ref 名 / 分支列表 / 解析结果） | 抛 `GitOutputUndecodableError` → `NOT_SUPPORTED`，**绝不** mangled 回传 |
| `"replace"` | 内容类输出（patch 正文 / 文件名 / numstat 文件名） | 出现 `�`、**不失败**（只进 DOM、永不回传 git） |

为什么出站标识符必须严格：mangled 的名字回传 git 会变成 `BAD_REF`，**甚至指向别的 ref**——
比报错更糟。`repo.branches` / `repo.bind` 两条路径的码映射在 `api/repository.py`；
回归测试是 `tests/test_diff.py` 的 D10 第 19/20 行。

### S5：diff 的三条命令与「影响输出的用户配置全部用 flag 钉死」（D1/D2/D4/D5）

```bash
# ① 解析：每个 ref 各调一次（--verify 只能收一个 rev），用户字符串只在这里出现
git -C <root> rev-parse --verify --end-of-options <ref>^{commit}
# ② 统计：三点语义 + 确定性 flag；-z 必须（路径可含 tab/换行，T36）
git -C <root> -c core.quotePath=false diff --numstat -z <STAT_FLAGS> <a>...<b>
# ③ patch：同一对 object id，%DIFF_FLAGS% 全部钉死
git -C <root> -c core.quotePath=false diff <DIFF_FLAGS> <a>...<b>
```

| 决策 | 理由（都有实测，见 06 T34/T36） |
|---|---|
| 先解析成 object id，再用两个 id 跑 stat/patch | ① 注入面收敛到一条带 `--end-of-options` 的命令；② base/head 解析后不再"移动"，统计与 patch 描述同一对 commit |
| `^{commit}` 强制类型 | tree id 拿来问会回 `expected commit type` + exit 128 → `BAD_REF`；没有它，tree id 会被当 commit 用 |
| 形状自己校验（`_OBJECT_ID`） | 没有 `--verify` 时 rev-parse 会把有歧义的参数**原样回显**到 stdout 且 exit 128——"回显"不是"解析结果" |
| `--numstat` 后再补 `--unified` = 灾难 | 实测 git 2.53：`--unified` 隐含 `--patch`，把 `--numstat` 隐含的 `--no-patch` 覆盖回来，输出「numstat 记录 + 空行 + 完整 patch」——按 NUL 切会把 patch 当记录（所以有独立的 `STAT_FLAGS`，T36） |
| `-z` 而不是按行解析 | 路径可含 tab 甚至换行（实测 `new\nline.txt` 原样进 NUL 分隔记录）；`-z` 之后每条记录只取前两段（`add\tdel\tpath`），路径怎么脏都不影响 |
| patch / 文件名用 `decode_errors="replace"` | 内容只进 DOM、永不回传 git；仓库里有非 UTF-8 内容是常态。**ref 名**仍 strict（D3.2） |

`diff_stat` / `diff_patch` 的三态与 `list_branches` 同构：`DiffStat`/`str`（可能是空）=
git 答了；`None` = 非零退出（无共同祖先等）；`GitUnavailableError` / `GitTimeoutError` 由
调用方翻码。**空差异不是错误**（`DiffStat(0,0,0)` / `""`）。

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

#### T33：`%(HEAD)` 的空格是**真值**，整段 strip 只改最后一行的 marker（详见 [06 T33](../06-pitfalls.md)）

`%(HEAD)` 对**非当前**分支输出的是**一个空格**（不是空串，而且**是真值**），当前分支是 `"*"`；
而 `_stdout()` 对**整段** stdout `.strip()`，只会把**最后一条记录**的 marker 从 `" "` 变成 `""`
——**字段数恒为 4**（`%00` 是 NUL、不是空白，行尾 NUL 不会被 strip）。所以"字段会消失 / 只剩
3 段"是**错的**，别按那个写解析器，也别按那个写复现命令。

解析必须：① 字段各自 `.strip()`，当前判定写 `marker == "*"`，**不要**用真值判断
（未 strip 的 `" "` 是真值 → 除最后一条外全判反；而最后一条被 strip 成 `""` 反而碰巧判对）；
② `len(fields) != 4` 只可能是 format 被改写：返回 `None`（"问不出来"），别猜；
③ 测试断言**每一条**分支的 `is_current`，并覆盖"当前分支不是最后一条"与"最后一条带
upstream"两种布局（前者露 bug、后者覆盖整段 strip 的路径）。
回归测试：`tests/test_branches.py` 的 D7 第 2/3/7 行；反向验证：把 `marker == "*"` 注入成
`bool(fields[3])` → 第 2 行真的变红（`tests/test_reverse_checks.py`）。

### 被推翻的方案

| 方案 | 为什么推翻 |
|---|---|
| 直接 `subprocess.run(["git", ...])`（裸命令名） | `PATH` 被清空时 `FileNotFoundError` 才能被发现；但裸名会在 `PATH` 里有影子 git 时静默跑错东西。改 `which` + 绝对路径 |
| 用 `git rev-parse --show-toplevel` 当 `is_repo` 的判据 | git 会 discover 到父目录 → 普通目录被判成仓库（T25）。保留函数但**不作为判据** |
| 用 `git status --porcelain` 判 `isDirty` | 07 §4.1 示例里有 `isDirty`，但 S3 不需要，且它和"是不是仓库"无关。**不填假字段**，留给 S4 |
| 把超时设成 30 秒 | D5 建议 5 秒；本地只读命令毫秒级，30 秒等于"卡住时整个网关被占 30 秒"。**只读的** 30 秒不行；写面（S7）另算，见下 |

## 写面（`gitops/worktrees.py`，S7）

S7 是内核第一次往用户仓库里写东西。全部写操作就在这一个文件里，且只有三个命令
（`worktree add` / `remove` / `prune`）——**这是允许清单，不是示例**。

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `WORKTREE_TIMEOUT_SECONDS` | 42 | `30.0`：写操作的超时（读是 5 秒——检出整棵树不是毫秒级） | `_run` |
| `BRANCH_PREFIX` / `WORKSPACE_LEAF` | 44–45 | `bot` / `workspace`：S7 的占位分支 `bot/<bot>/workspace` | `api/worktrees.py` |
| `_PRUNED_PREFIX` | 46 | `prunable`：只比**前缀**，因为后面那半句是本地化文案（实测中文） | `list_worktrees` |
| `GitRefusedError` | 49 | git 非零退出（"我拒绝"）。**不带 git 的输出**（本地化 + 可能含用户路径） | `api/worktrees.py` |
| `WorktreeEntry` | 58 | frozen dataclass：`path` / `branch`（短名）/ `head_sha` / `prunable` | `list_worktrees`、服务层 |
| `_run(args, *, root, timeout=)` | 71 | 写操作的外壳：字面数组 + `shell=False` + `git_env()` + 超时；非零退出抛 `GitRefusedError` | 本模块全部函数 |
| `list_worktrees(root)` | 105 | `worktree list --porcelain` → `list[WorktreeEntry]`（按字段前缀解析） | 服务层三处 |
| `add_worktree(root, target, *, branch, start_point, create_branch)` | 136 | 建分支（`-b` + 起点）或检出**已存在**的分支 | 服务层 `acquire` |
| `remove_worktree(root, target)` | 163 | `worktree remove`，**没有 `--force`** | 服务层 `release` |
| `prune_worktrees(root)` | 172 | 只清"目录已经没了"的元数据 | 服务层（`prunable` 的两条路径） |
| `branch_exists(root, branch)` | 180 | 本地分支在不在（决定 `-b` 还是检出） | 服务层 `acquire` |
| `is_dirty(worktree)` | 189 | `status --porcelain` 非空 = 有会被丢掉的东西（判不出来也按脏处理） | 服务层 `release` 的拒绝翻译 |
| `GIT_WRITE_ERRORS` | 207 | 三个异常的元组（"能抛什么"的显式清单） | 服务层 |

四条值得记住的理由：

1. **不加 `--force`**：实测脏 worktree 的 `remove` 退出 128、加 `--force` 就删。宁可失败。
2. **不解析本地化文案**：`prunable gitdir 文件指向一个不存在的位置` 是中文；按文案判会在别的
   locale 静默失效（T39 的同一族）。判定来自文件系统 + 字段**前缀**。
3. **`create_branch=False` 时必须把分支名传给它，不能传 sha**：传 sha 会静默检出成游离 HEAD
   ——分支上的提交"消失"，`worktree list` 的 `branch` 字段也消失。实测踩过，见 [T40](../06-pitfalls.md)。
4. **超时 30 秒**：`worktree add` 要检出一整棵树；5 秒的读超时会在正常仓库上误报。

## 修改须知

| 如果你要… | 还波及 |
|---|---|
| 加一个新的 git 探测 | 用 `run_git`；**不要**自己 `subprocess.run`。要 `test_bind.py` / `test_branches.py` 里有一条真实仓库用例 |
| 改 `BRANCH_FORMAT` 或它的解析 | T33（`%(HEAD)` 空格 + 整段 strip）、D7 第 1/2/3/5/7 行；`test_branches.py` 全部解析用例 |
| 改 `_environment()` | 05 §3.3 / S3 D5；`test_bind_uses_git_in_a_way_that_cannot_prompt` 会红 |
| 改 `DIFF_FLAGS` / `STAT_FLAGS` | S5 D4 + 06 T34/T36；反向注入 D12 #1/#2（删 `--no-color` / `--no-ext-diff` → `test_patch_has_no_ansi_*` / `test_patch_ignores_external_diff_driver` 必须红）；**不要**把 `--unified` 加进 `STAT_FLAGS` |
| 改 `resolve_commit` 的命令形状 | S5 D2 + 反向注入 D12 #4（去掉 `^{commit}` → `test_bad_refs_are_bad_ref` 必须红）；**不要**合并成一次 `--verify` 多 rev（git 会报「需要一个单独的版本」） |
| 改 diff 的解析 | S5 D5/D6、06 T36；`tests/test_diff.py` 的 stat/截断/重命名/二进制/mode 用例 |
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

`tests/test_diff.py` 里与 gitops 层直接相关的用例（S5）：

| 测试 | 守什么 |
|---|---|
| `test_stat_exact_numbers` / `test_three_dot_ignores_later_base_commits` | 三点语义与 numstat 解析（D1/D5） |
| `test_stat_counts_binary_file_without_line_counts` / `test_stat_counts_mode_only_change` / `test_rename_counts_as_delete_plus_add` | `-` 记 0 / mode-only / `--no-renames` 的一删一增（D4/D5） |
| `test_patch_has_no_ansi_when_color_ui_always` / `test_patch_ignores_external_diff_driver` | 确定性 flag（D4，反向注入 D12 #1/#2） |
| `test_non_utf8_content_is_replaced_not_fatal` | 内容宽松解码（D3.3） |
| `test_non_ascii_path_is_readable_not_escaped` | `core.quotePath=false`（D4） |
| `test_bad_refs_are_bad_ref` | `^{commit}` + 形状校验（D2，反向注入 D12 #4） |
| `test_unrelated_histories_are_bad_ref` | 三点无 merge base → `BAD_REF`（D1/T35） |

`tests/test_branches.py` 里与 gitops 层直接相关的用例：

| 测试 | 守什么 |
|---|---|
| `test_branches_returns_names_in_refname_order_with_short_shas` | D7 第 1 行 / D1 / D4：名称集合、字典序、gitops 层 `sha` 是完整 40 位（帧层截 8 位在 `api/repository.py`） |
| `test_branch_current_flag_is_only_on_the_head_branch` | D7 第 2 行 / D1：只有 HEAD 指向的那条 `is_current=True` |
| `test_branch_upstream_is_short_name_and_none_when_missing` | D7 第 3 行 / D1：`upstream` 有则短名、无则 `None`（不是 `""`）；同时覆盖"当前分支恰是最后一条且有 upstream"的布局（整段 strip 只改最后一行的 marker，字段数恒为 4） |
| `test_empty_repo_returns_empty_list_as_success` | D7 第 4 行 / D4：空列表是成功 |
| `test_detached_head_still_lists_all_branches_with_none_current` | D7 第 5 行 / D1 |
| `test_only_local_branches_are_listed_after_push` | D7 第 6 行 / D2 |
| `test_parsing_does_not_depend_on_human_readable_output` | D7 第 7 行 / T33：`color.ui always` + 非 ASCII 名（`功能/中文`） |
| `test_list_branches_returns_none_for_non_repo_and_missing_path` | D7 第 10 行 / D4：三态里的 `None` |


## S11 扩展的写面（仍然只有这一个文件）

S7 D5 的允许清单**照旧**，S11 D5 只往里加三条，不删任何禁止项：

| 符号 | 行 | 做什么 |
|---|---|---|
| `_COMMIT_IDENTITY` | 212 | `-c user.email=kernel@localhost -c user.name=postel`——提交身份来自内核，不读用户 git config（T34） |
| `head_commit(worktree)` | 215 | 只读 `rev-parse HEAD`，但复用 30s 外壳与环境；任务开始/结束各调一次（S11 D2） |
| `commit_all(worktree, message)` | 224 | `git add -A` + `git commit -m <内核生成的消息>`，只作用于**Bot 工作区** |
| `create_branch(root, branch, start_sha)` | 237 | 在**主仓库 refs** 建候选分支；分支已存在时 git 拒绝，不覆盖 |

禁止项一字未改：无 `--force`、无 `reset --hard` / `clean` / `stash`、不删分支、不在主工作区
checkout、内核不直接写工作区文件（Agent 写、内核只 stage/commit）。复用工作区**不重置**，
所以 S11 以"任务开始时的 HEAD"为 diff base，见 [S11 spec §2 D2](../../design/issues/S11-candidate-emerge.md)。
