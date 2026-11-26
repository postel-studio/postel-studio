# S1 · 内核存活即可见

- **里程碑** M1 · **预计** 2h · **依赖** 无 · **优先级** P0
- **用户路径**：终端运行 → 浏览器打开地址 → 看到「内核存活 v0.1.2」

## 背景

一切的前提：能启动、能连上、能看到自己活着。这是唯一一个"不碰任何业务"的切片，此后每刀都要带业务。

## 验收标准

1. `postel-studio serve` 在前台常驻运行，打印可点击的 `http://127.0.0.1:<port>`。
2. 浏览器访问该地址，页面显示内核版本与状态根路径。
3. 端口由内核分配（`--port 0` 默认），并写入 `~/.postel/server.json`，权限 `600`。
4. `Ctrl+C` 退出后 `server.json` 被删除，无残留进程。
5. 二次启动时若旧 lock 存在但 pid 已死，能正常启动并覆盖；pid 存活则拒绝启动并提示。

## 涉及文件

- `src/postel_studio/api/server.py`（新建，≤120 行）
- `src/postel_studio/api/app.py`（新建，仅 `/` 与 `/api/health`）
- `src/postel_studio/storage/paths.py`（新建，`state_root` 与 `server.json` 读写）
- `src/postel_studio/web/assets.py`（新建，静态 HTML）
- `src/postel_studio/__init__.py`（`serve` 子命令接上真实现）
- `tests/test_server_boot.py`（新建）

## 测试要求

- 集成测试起**真实子进程**：读 lock → 真实 HTTP 请求 `/api/health` → 发 SIGTERM → 断言 lock 已删。
- 断言 lock 文件权限为 `600`。
- 断言 pid 存活时二次启动失败（`BUSY_PORT` 或类似稳定错误）。

## 禁止事项

- 不得绑定 `0.0.0.0`（只允许 `127.0.0.1`）。
- 不得用固定端口。
- 不得在页面里渲染任何凭据或绝对路径以外的敏感信息。
- 不得引入前端构建链（本刀只允许一个 HTML 字符串）。
