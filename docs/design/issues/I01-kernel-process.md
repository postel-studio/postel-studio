# I01 · 常驻内核进程 + 端口发现

- **里程碑**：M1　**预计**：2h　**依赖**：无　**优先级**：P0

## 背景

GUI 只是内核的一个客户端。若 GUI 持有 worktree 与 Agent 子进程，窗口一关 Bot 就死，「常驻」不成立。本切片先让内核能独立启动、能被发现。

## 验收标准

1. `postel-studio serve` 启动后常驻，不因终端输入结束而退出。
2. 启动时把 `{host, port, pid, apiVersion}` 写入 `~/.postel/server.json`，文件权限 `600`。
3. `--port 0` 时由内核分配空闲端口，写入的值与实际监听一致。
4. 收到 `SIGTERM` / `SIGINT` 后**删除** `server.json` 并退出，退出码 0。
5. 已存在 `server.json` 且 pid 仍存活时，再次启动必须拒绝并提示，不覆盖文件。

## 涉及文件

- `src/postel_studio/api/server.py`（新建）
- `src/postel_studio/storage/paths.py`（新建）
- `src/postel_studio/__init__.py`（增加 `serve` 子命令）
- `tests/test_server_lock.py`（新建）

## 测试要求

- 单测：`server.json` 写入字段齐全、权限 600；`--port 0` 时写入值可被连接。
- 集成：起进程 → 读 lock → 真实 HTTP 握手成功 → 发 SIGTERM → 断言 lock 已删。
- 不得用 mock 替代真实进程启动（至少一条集成测试走真实子进程）。

## 禁止事项

- 不得在 lock 文件里写任何凭据或绝对路径以外的敏感信息。
- 不得用固定端口（默认必须 0，由内核分配）。
- 不得让进程在前台等待 stdin 才能运行。
