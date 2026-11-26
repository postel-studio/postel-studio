# 03 · domain — `src/postel_studio/domain/`

> 层 L0（纯函数，**无 IO**）· 168 行 · 上游：[模块索引](README.md) · 相关：[06-data-model §2.1](../../design/06-data-model.md) · [08-repo-id-stability](../../design/08-repo-id-stability.md)

## 职责

领域模型与**纯派生规则**：`Repo` / `Bot` 实体、origin URL 归一化、`repo_id` / `bot_id`
与名字白名单的算法（S6 加了 Bot）。
这一层**不碰磁盘、不起子进程、不 import 框架**——它的每个函数都能在一行断言里被验证。

S3 把它单独成层而不是塞进 `api/repository.py` 的原因：id 的算法是被验收断言过的契约
（S3.5 D1：长度 12、只由路径决定；D2：https / ssh 归一化后 `origin_hash` 相同）。
如果它和 git、SQLite 长在一起，验证这些不变量就得先建仓库、再建数据库。

## 文件

| 文件 | 行数 | 内容 |
|---|---|---|
| `__init__.py` | 5 | 包标记 + 本层"无 IO"的声明 |
| `models.py` | 168 | `Repo` / `Bot` / `normalize_origin` / `repo_id*` / `origin_hash` / `bot_name_ok` / `bot_id` |

## 符号表（`domain/models.py`）

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `REPO_ID_LENGTH` | 22 | `12`：06 §2.1 的 id 长度 | `models.repo_id*`、测试 |
| `_SCP_LIKE` / `_URL_LIKE` | 26-27 | 两种 remote 写法的正则 | `normalize_origin` |
| `Repo` | 31 | frozen dataclass：`repo_id` / `path` / `origin_url` / `default_branch` / `added_at` / `origin_hash`（**末位、有缺省值**） | `storage/db.py`、`api/repository.py` |
| `Repo.name`（property） | 48 | 根目录名 = 左栏显示名（`proj/src` 绑成 `proj` 后显示 `proj`） | `api/repository.py:_split` |
| `normalize_origin(url)` | 53 | URL → 与传输方式无关的 `host/path` | `repo_id_for_origin`、测试 |
| `repo_id_for_origin(url)` | 90 | `sha256(归一化 URL)[:12]`——**只给 `origin_hash` 用**（S3.5 D2） | `origin_hash` |
| `repo_id_for_path(path)` | 100 | `sha256(绝对路径)[:12]`——`repo_id` 的**唯一**算法（D1） | `repo_id` |
| `origin_hash(origin_url)` | 111 | 归一化 URL 的哈希；无 origin → `None`（D2，**不参与主键**） | `api/repository.py:bind` |
| `repo_id(path)` | 124 | **只看路径**（S3 的签名是 `repo_id(path, origin_url)`，已收窄） | `api/repository.py:bind` |
| `BOT_NAME_RE` | 139 | 06 §2.2 白名单 `^[a-z][a-z0-9-]{0,31}$` | `bot_name_ok`、`storage/paths.py` |
| `BOT_PERMISSION_DEFAULT` | 141 | `"L1"`：M1 的 Bot 权限恒为默认值（改权限是 M2） | `api/bots.py:_seed` |
| `Bot` | 145 | frozen dataclass：`bot_id` / `repo_id` / `name` / `permission` / `agent_kind` / `charter_rev` / `created_at` | `storage/db.py`、`api/bots.py` |
| `bot_name_ok(name)` | 161 | 名字白名单判定（**纯函数**）；进路径前必须过它（D3） | `storage/paths.py:bot_memory_dir` |
| `bot_id(repo_id, name)` | 166 | 复合主键 `{repo_id}/{name}`——**唯一的拼接处**（06 §2.2） | `api/bots.py:_seed`、测试 |

## 关键决策与理由

### `normalize_origin` 只抹平"传输方式"，不抹平别的一切

| 输入 | 归一化 |
|---|---|
| `https://github.com/o/p.git` | `github.com/o/p` |
| `git@github.com:o/p.git` | `github.com/o/p` |
| `ssh://git@github.com:22/o/p.git` | `github.com/o/p` |
| `https://GitHub.com/o/p/` | `github.com/o/p` |
| `/srv/git/p.git`（本地路径 remote） | `/srv/git/p.git`（原样） |

去掉的：scheme、`user@`、端口、尾部 `.git`、首尾 `/`；host 统一小写。
**不做 URL 解码**：解码只会把两种来源的差异放大。
**本地路径 remote 原样返回**：它不是 URL，猜它的"仓库身份"没有意义。

### 为什么 `repo_id` 只看路径，origin 降级成 `origin_hash`（S3.5 D1/D2，**U9 已结案**）

S3 的算法是"有 origin 用 URL 哈希、没有才用路径"。后果实测过：`git remote add origin …`
再重绑，同一个仓库的 `repo_id` 就变了——S4 起 `bot_id`、记忆目录、分支列表全挂在 id 上，
会一起失效（[08 §1](../../design/08-repo-id-stability.md) 的实测输出）。

现在拆成两个字段：

| 字段 | 算法 | 稳定性 | 参与主键 |
|---|---|---|---|
| `repo_id` | `sha256(绑定时 resolve 的绝对路径)[:12]` | **永不更新**（`upsert` 不写它） | 是 |
| `origin_hash` | `sha256(normalize_origin(url))[:12]` | 可变（origin 变它就变） | **否** |

**为什么不随机生成（B2）**：保留"从路径能算出来"的可重建性（06 §5：DB 丢失后可从文件
系统重建索引），可解释、可复现。代价是"仓库被移动到新路径后 id 会变"——比"加 remote"
罕见得多，且移动仓库本来就需要人确认。

**注意 `repo_id()` 的签名收窄了**：S3 是 `repo_id(path, origin_url)`，现在 `repo_id(path)`。
把 origin 传进来就等于允许"origin 变了 id 就变"，少一个参数就少一条错路。

### Bot 实体与名字白名单（S6 D7）

`Bot` 是 06 §2.2 的实体，**纯数据类**；`bot_id()` 只做一次 `{repo_id}/{name}` 拼接，
`bot_name_ok()` 就是白名单正则。三者都不碰 IO，所以名字规则（`../x`、`Builder`、33 字符
必须被拒）可以在 L0 用一行断言钉死，不用建数据库。

**`charter_rev` 在实体里，但 M1 不拿它当事实源**：D5 规定 charter 正文的事实源是文件，
帧载荷里的 `charterRev` 由 `api/bots.py` 现场算；DB 里这一列只记播种那一刻的值（U17）。

### 为什么 `name` 是 property 而不是字段

`name` 是**派生**的（`Path(path).name`），落库的只有 `path`。存两列会让"路径改名"时
两列打架。06 §2.1 的 Repo 表里也确实没有 `name` 列。

## 修改须知

| 如果你要… | 还波及 |
|---|---|
| 改 `normalize_origin` | `origin_hash` 的所有已有值（含 v1 迁移后的 NULL → 将来若回填）会变；`repo_id` **不受影响**（它不看 origin） |
| 改 `repo_id` 算法 | **所有已存在的 `repo_id`** 都会失配 → 必须配一次迁移，且**不允许**回写历史 id（D3 的教训，见 08 §3） |
| 改 `upsert` 的规则 1 | D1 的"写后永不更新"会破 → `test_rebind_*` + `test_each_half_of_u9_alone_*` 会亮 |
| 给 `Repo` 加字段 | `storage/db.py` 的 DDL（`_MIGRATIONS` 追加一版）+ `api/repository.py:_split` 的帧字段 |
| 改 Bot 名字白名单 | `storage/paths.py:bot_memory_dir` 的路径安全（D3）+ `tests/test_bot_charter.py` + 06 §2.2 |
| 在本层 import `gitops` / `storage` | **禁止**：会把它从"无 IO 的纯函数层"变成"有依赖的层"，分层表也要改 |

## 测试

| 测试 | 守什么 |
|---|---|
| `tests/test_bind.py::test_repo_id_normalisation_edge_cases` | `normalize_origin` 的每一条规则 |
| `tests/test_bind.py::test_https_and_ssh_origins_share_the_same_repo_id` | D2：https/ssh → `origin_hash` 相同、id 不同（真实 git） |
| `tests/test_bind.py::test_rebinding_after_origin_added_updates_the_row` | D1：加 origin 后 id **不变**、旧 id 仍可取（验收 2/3） |
| `tests/test_bind.py::test_double_bind_is_idempotent_row_and_id` | 幂等：两次 bind 行/id 都不变 |
| `tests/test_bind.py::test_origin_hash_tracks_origin_changes_while_repo_id_does_not` | D2 的分工（可变 vs 不可变） |
| `tests/test_bind.py::test_repo_id_without_origin_is_derived_from_the_absolute_path` | id 来自路径 |
| `tests/test_bind.py::test_absolute_path_hash_is_stable_across_calls` | 纯函数稳定性 |
| `tests/test_reverse_checks.py`（`REPO_ID_LENGTH→16` / 去掉归一化 / U9 组合注入） | 改这些会让上面的测试红（反向锁） |
| `tests/test_bot_charter.py::test_bot_name_whitelist` | S6 D11 #11：`../x` / `Builder` / `a_b` / 33 字符被拒 |