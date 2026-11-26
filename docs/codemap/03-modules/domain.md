# 03 · domain — `src/postel_studio/domain/`

> 层 L0（纯函数，**无 IO**）· 110 行 · 上游：[模块索引](README.md) · 相关：[06-data-model §2.1](../../design/06-data-model.md)

## 职责

领域模型与**纯派生规则**：`Repo` 实体、origin URL 归一化、`repo_id` 的算法。
这一层**不碰磁盘、不起子进程、不 import 框架**——它的每个函数都能在一行断言里被验证。

S3 把它单独成层而不是塞进 `api/repository.py` 的原因：`repo_id` 是被验收断言过的契约
（D3：https / ssh 两种 URL 必须同 id；长度必须 12）。如果它和 git、SQLite 长在一起，
验证这条不变量就得先建仓库、再建数据库。

## 文件

| 文件 | 行数 | 内容 |
|---|---|---|
| `__init__.py` | 5 | 包标记 + 本层"无 IO"的声明 |
| `models.py` | 104 | `Repo` / `normalize_origin` / `repo_id*` |

## 符号表（`domain/models.py`）

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `REPO_ID_LENGTH` | 19 | `12`：06 §2.1 的 id 长度 | `models.repo_id*`、测试 |
| `_SCP_LIKE` / `_URL_LIKE` | 23-24 | 两种 remote 写法的正则 | `normalize_origin` |
| `Repo` | 28 | frozen dataclass：`repo_id` / `path` / `origin_url` / `default_branch` / `added_at` | `storage/db.py`、`api/repository.py` |
| `Repo.name`（property） | 42 | 根目录名 = 左栏显示名（`proj/src` 绑成 `proj` 后显示 `proj`） | `api/repository.py:_split` |
| `normalize_origin(url)` | 47 | URL → 与传输方式无关的 `host/path` | `repo_id_for_origin`、测试 |
| `repo_id_for_origin(url)` | 84 | `sha256(归一化 URL)[:12]` | `repo_id` |
| `repo_id_for_path(path)` | 90 | `sha256(绝对路径)[:12]`（无 origin 时的让步，D3） | `repo_id` |
| `repo_id(path, origin_url)` | 100 | 有 origin 用 URL，没有用路径 | `api/repository.py:bind` |

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

### 为什么 `repo_id` 有"无 origin 时用路径"的让步（D3）

06 §2.1 就是这么写的，S3 照做。**已知不完美**：仓库后来 `git remote add origin`，id 会从
路径哈希变成 URL 哈希 —— 同一个仓库在界面上"变了一个"。
- 本刀不做迁移（`storage/db.py:upsert` 会更新那一行的 `repo_id`，行数不增，但 id 变）；
- 测试 `test_rebinding_after_origin_added_updates_the_row` 把这个行为钉成可执行的事实；
- **S4 之前必须解决**，否则分支列表 / Bot 会挂在 id 上丢失。见 [06 §已知未决 U9](../06-pitfalls.md#已知未决问题)。

### 为什么 `name` 是 property 而不是字段

`name` 是**派生**的（`Path(path).name`），落库的只有 `path`。存两列会让"路径改名"时
两列打架。06 §2.1 的 Repo 表里也确实没有 `name` 列。

## 修改须知

| 如果你要… | 还波及 |
|---|---|
| 改 `normalize_origin` | D3 的不变量、`test_bind.py::test_https_and_ssh_origins_share_the_same_repo_id`，**以及所有已存在的 `repo_id`**（用户数据里的 id 会失配） |
| 改 `repo_id` 算法 | 同上 + 需要一个迁移（本刀没有迁移机制，见 U9） |
| 给 `Repo` 加字段 | `storage/db.py` 的 DDL（`_MIGRATIONS` 追加一版）+ `api/repository.py:_split` 的帧字段 |
| 在本层 import `gitops` / `storage` | **禁止**：会把它从"无 IO 的纯函数层"变成"有依赖的层"，分层表也要改 |

## 测试

| 测试 | 守什么 |
|---|---|
| `tests/test_bind.py::test_repo_id_normalisation_edge_cases` | `normalize_origin` 的每一条规则 |
| `tests/test_bind.py::test_https_and_ssh_origins_share_the_same_repo_id` | D3 的硬不变量（真实 git） |
| `tests/test_bind.py::test_repo_id_without_origin_is_derived_from_the_absolute_path` | 无 origin 分支 |
| `tests/test_bind.py::test_absolute_path_hash_is_stable_across_calls` | id 稳定（同路径同 id） |
| `tests/test_reverse_checks.py`（`REPO_ID_LENGTH→16` / 去掉归一化两条注入） | 改这两处会让上面的测试红（反向锁） |