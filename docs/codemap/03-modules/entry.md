# 03 · entry — `src/postel_studio/__init__.py`

> 层 L0 · 55 行 · 上游：[模块索引](README.md)

## 职责

CLI 入口。解析 argv，把 `serve` 交给 `api/server.py`，把 `version` 直接打印。

## 符号表

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `__version__` | 12 | **版本号的唯一事实源** | `server.py`、`app.py`、`paths.py`、`test_server_boot.py` |
| `main(argv)` | 15 | CLI 入口，返回退出码 | `pyproject.toml` 的 `[project.scripts]`、`test_smoke.py` |
| `LOOPBACK` | — | 从 `server.py` 导入（函数内） | — |

## 关键决策与理由

### 为什么 `main()` 接受 `argv`

让测试**直接调用**而不用起子进程。`test_smoke.py` 就是这么测参数校验的。
副作用是 `main()` 可以被反复调用（无全局状态），**新加子命令时不要引入模块级可变状态**。

### 为什么 `serve` / `paths` 的函数内 import

让 `postel-studio version` 和 `--help` **不必加载 fastapi/uvicorn**。

`srv` 相关 import 放在 `if args.cmd == "serve":` 里面，所以：
- `postel-studio version` → 只加载 argparse（毫秒级）
- `postel-studio serve` → 才加载 fastapi/uvicorn

**别把这两个 import 挪到模块顶部**——会拖慢一个纯打印命令。

### 为什么 `--host` 的校验在 CLI 层而不在 `server.py`

`socket.bind` 那里其实已经硬编码 `LOOPBACK`，绑不到别的地址。CLI 这一层是**为了给出
清晰的错误码**（`HOST_NOT_ALLOWED`，退出码 2），而不是让用户看到底层 socket 报错。

两道关：CLI 层拒绝（给人看）+ `server.listen()` 只绑 loopback（真正的保障）。
**不要只留一道**——只留 CLI 那道，将来有人直接调 `serve()` 就绕过了。

### 退出码约定

| 码 | 场景 |
|---|---|
| `0` | 正常（含 `version`、`--help`） |
| `1` | `KERNEL_ALREADY_RUNNING` / `LISTEN_FAILED` / `PORT_INVALID` |
| `2` | `HOST_NOT_ALLOWED`（参数类错误，argparse 也走 2） |

见 [07-api-contract §1.3](../../design/07-api-contract.md)。

## 修改须知

| 如果你要… | 还要改 |
|---|---|
| 加子命令 | `test_smoke.py`；`README.md` 的用法段 |
| 改版本号 | **只改这里**；`pyproject.toml` 的 `version` 另有一份（`uv_build` 从 pyproject 取），两处需同步 —— 见下 |
| 改退出码 | `07-api-contract §1.3` 的表 + `test_smoke.py` |

> **版本号有两个来源**：`pyproject.toml:version`（打包用）和 `__init__.py:__version__`（运行时用）。
> 它们是**同一个数字的两份副本**，发版时必须一起改。S20 会把这件事自动化（或加一条校验测试）。
> 曾经在 `paths.py` 里出现过第三份（硬编码 `_VERSION`），S1 审核时删掉了。

## 测试

| 测试 | 守什么 |
|---|---|
| `tests/test_smoke.py` | `version` 输出、`--host 0.0.0.0` → 退出码 2、`--port -1` → 退出码 1 |