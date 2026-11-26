# 03 · paths — `src/postel_studio/storage/paths.py`

> 层 L0 · 273 行 · 上游：[模块索引](README.md) · 相关：[01-overview §4](../01-overview.md#4-状态根长什么样)

## 职责

**唯一碰磁盘的地方。** 状态根、`server.json`、锁、令牌、原子写、权限收紧；S6 起还负责
Bot 的记忆空间（`bots/<repo_id>/<name>/`）与 charter 模板。

它**不判断进程是否存活**（那是 `api/server.py` 的职责），**不处理信号**。
这个边界让 paths 的每个函数都能被单元测试安全调用。

## 符号表

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `ENV_HOME` / `SERVER_FILE` / `LOCK_FILE` | 21-23 | 常量名 | |
| `DIR_MODE` `0o700` / `FILE_MODE` `0o600` | 25-26 | 权限 | 全部写操作 |
| `_chmod(path, mode)` | 29 | POSIX 才收紧；Windows 跳过 | 内部 |
| `state_root()` | 35 | 返回状态根（**不创建**） | `app.py`、`server.py`、测试 |
| `ensure_state_root()` | 43 | 创建 + 收紧 0700 | `server.serve()` |
| `server_file()` / `lock_file()` / `token_path()` | 51/56/61 | 三个路径 | 内部与 server |
| `atomic_write(path, data)` | 66 | mkstemp + fsync + replace | 内部 |
| `write_server_info` / `read_server_info` / `remove_server_info` | 84/90/99 | server.json CRUD | server、测试 |
| `write_token` / `remove_token` / `fingerprint` | 104/111/117 | 令牌文件与指纹 | server |
| `ERR_ALREADY_RUNNING` | 126 | `"KERNEL_ALREADY_RUNNING"` | server、测试 |
| `KernelBootError(code, message)` | 129 | 拒绝启动的异常，带稳定码 | CLI、server |
| `pid_alive(pid)` | 138 | `kill(pid,0)` 探测 | `acquire_lock` |
| `read_lock_pid(path)` | 151 | 坏锁返回 `None` | `acquire_lock`、`release_lock` |
| `_lock_payload()` | 161 | 锁内容 JSON | 内部 |
| `acquire_lock(path, force=)` | 169 | O_EXCL 抢锁 / 覆盖陈旧锁 | `server.serve()` |
| `release_lock(path)` | 192 | **只删自己的**锁 | `server.serve()` |
| `BOTS_DIR` / `CHARTER_FILE` / `MEMORY_DIR` / `SESSIONS_DIR` | 216-219 | `bots` / `charter.md` / `memory` / `sessions` 四个文件名的事实源 | 内部、`api/bots.py` |
| `CHARTER_TEMPLATE` | 221 | D4 的空白骨架（`# {name}` + 四节标题，**不含任何职责**） | `ensure_memory_space`、测试 |
| `bots_root()` | 233 | `<状态根>/bots`（不创建） | `bot_memory_dir`、测试 |
| `bot_memory_dir(repo_id, name)` | 238 | 拼记忆目录；**白名单 + resolve 越界断言**（D3） | `api/bots.py` |
| `ensure_memory_space(repo_id, name)` | 252 | 缺什么建什么（0700）+ 缺 charter 才写模板（0600，幂等） | `api/bots.py` |
| `read_charter(repo_id, name)` | 271 | 读 charter 正文（缺失抛 `FileNotFoundError`） | `api/bots.py` |

## 关键决策与理由

### `state_root()` 的 `POSTEL_HOME` 覆盖是测试隔离的地基

```python
override = os.environ.get(ENV_HOME, "").strip()  # 空串按缺省处理
```

- **空串按缺省**：`POSTEL_HOME=""` 不应该变成「当前目录」，否则误设环境变量就会把状态
  写进仓库根目录。
- 测试靠它把状态根指到 `tmp_path`，`tests/conftest.py` 还额外把 `Path.home` 也改掉，
  做双保险。**改这个函数会让全部 77 个测试失去隔离。**

### `atomic_write` 为什么这么麻烦

```
mkstemp(同目录) → chmod 0600 → write → flush → fsync → replace
```

是为了**读到的永远是完整文件**。直接 `open("w")` 写会让并发读者看到半截 JSON。
`server.json` 正是被「看见即等价于内核已在听」的信号（02b §2.1），半截文件会让客户端崩溃。

`fsync` 那一步不能省：`replace` 只保证原子替换，不保证数据落盘。

### `_lock_payload()` 为什么在函数体内 import `__version__`

**为了版本号只有一个事实源。** 锁文件要写版本号，而 `__version__` 只允许定义在包根
（`__init__.py:12`）。模块级 `from postel_studio import __version__` 会成环
（包根 → server → app → … → paths）。

**这是正当的函数内 import**（单一事实源），与 S2 里那个为了绕开分层错误的函数内 import 不同。
判断标准见 [02-layers §2.1](../02-layers.md#21-两处函数内-import的理由不同不要混为一谈)。

> 曾经有人（S1 实现）在这里硬编码 `_VERSION = "0.1.2"` 并声称「避免 import 环」。
> 实测那个环不成立，且会让发版漏改。已删除。**不要恢复。**

### `pid_alive(pid)` 为什么要判 `pid <= 0`

```python
if pid <= 0:
    return False
```

`os.kill(pid, 0)` 在 `pid=-1` 时会发给**进程组**，在 `pid=0` 时发给**当前进程组**。
锁文件被损坏成这两种值时，一个「探测」就会变成广播信号。负值一律视为不可信。

### `acquire_lock` 的三条分支

| 分支 | 行为 |
|---|---|
| O_EXCL 成功 | 新锁，写自己的 pid |
| 锁在 + pid 存活 + 无 `force` | 抛 `KERNEL_ALREADY_RUNNING` |
| 锁在 + pid 存活 + 有 `force` | 打警告到 stderr，原子覆盖（**不删对方的锁**） |
| 锁在 + pid 已死 / 锁是坏 JSON | 原子覆盖（按陈旧处理） |

**「坏锁按陈旧处理」是有意的失败即放行**：锁文件损坏时宁可让新内核起来，也不要用户
被一个坏文件永久锁死。

### Bot 的记忆空间：权限必须显式 chmod（S6 D3 / T38）

```text
<状态根>/bots/<repo_id>/<name>/
├── charter.md      # 0600（atomic_write：mkstemp → 0600 → rename）
├── memory/         # 0700
└── sessions/       # 0700
```

**实测（umask 022，见 06-pitfalls T38）**：`mkdir(parents=True, mode=0o700)` 的 `mode`
**只作用于叶子**，父级按 umask 给 0755；`Path.write_text` 建出来是 0644。所以
`ensure_memory_space` 对**每一层**建完都显式 `_chmod(0o700)`，写 charter 走 `atomic_write`。
`bot_memory_dir` 先过 `bot_name_ok` 白名单，再断言 `resolve()` 仍在 `bots_root()` 之下
（纵深防御：`..` 恰好 resolve 回 bots 根，挡住它的是白名单）。

`ensure_memory_space` 的幂等语义：**charter 已存在就只读不写**——绝不覆盖用户编辑（D2/D3）。

### `release_lock` 只删自己的锁

```python
if read_lock_pid(path) == os.getpid():
    path.unlink(missing_ok=True)
```

因为 `--force` 接管时，旧内核可能还活着，它退出时不能把**新内核的锁**删掉。

## 修改须知

| 如果你要… | 还波及 |
|---|---|
| 加新的状态文件 | 这个文件 + `05-security §4.1`（权限表）+ `tests/test_server_boot.py`（权限断言） |
| 改 `server.json` 字段 | `02b §2.1` 字段表 + `01-overview §4` + 任何消费它的客户端 |
| 改 `atomic_write` | 全部写路径都经过它；`test_server_boot.py` 有 0600 断言 |
| 让 paths 调 server 的函数 | **禁止**，会破坏 L0/L2 分层 |
| 改 Bot 记忆目录的权限 | `05-security §4.1` + `tests/test_bot_charter.py` 的 stat 断言 + 反向注入 2 条（D12） |
| 用 `write_text` 写 charter | **禁止**：0644 且非原子（T38）；用 `atomic_write` |

## 测试

| 测试 | 守什么 |
|---|---|
| `tests/test_server_boot.py::test_server_json_mode_is_600_and_state_root_is_700` | 权限收紧 |
| `...::test_refuses_to_start_when_lock_pid_is_alive` | 存活 pid 拒绝启动 |
| `...::test_overwrites_stale_lock_when_pid_is_dead` | 陈旧锁覆盖 |
| `...::test_garbage_lock_is_treated_as_stale` | 坏锁不锁死 |
| `...::test_acquire_lock_overwrites_when_forced` | `--force` 语义 |
| `...::test_pid_alive_rejects_non_positive_values` | `pid<=0` 防护 |
| `...::test_server_json_does_not_carry_secrets_or_token` | 令牌不进 server.json |
| `tests/test_bot_charter.py::test_ensure_memory_space_layout_and_permissions` | S6 D11 #4/#5：布局 + 0700/0600（真 stat） |
| `tests/test_bot_charter.py::test_ensure_memory_space_never_overwrites_an_existing_charter` | S6 D11 #2：幂等不覆盖 |
| `tests/test_bot_charter.py::test_ensure_memory_space_rejects_names_that_escape_bots_root` | S6 D11 #10：白名单 + 越界 |

> ⚠️ `test_overwrites_stale_lock_when_pid_is_dead` 在 S2 期间报过**一次**偶发失败
> （`FileNotFoundError: server.json`），之后连跑 12 轮未复现。**未定位根因**，见
> [06-pitfalls §已知未决](../06-pitfalls.md#已知未决问题)。