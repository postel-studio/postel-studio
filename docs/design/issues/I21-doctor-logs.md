# I21 · 诊断出口：日志与 doctor 命令

- **里程碑**：M3　**预计**：3h　**依赖**：I01　**优先级**：P2

## 背景

常驻进程出问题时，用户需要一条命令自查，而不是翻源码。

## 验收标准

1. `postel-studio doctor` 输出：内核是否存活、端口、`POSTEL_HOME`、SQLite 可写、git 可用、已装 Agent（opencode / dsh / pi）探测结果。
2. 每项给出 `ok / warn / fail` 与修复建议。
3. 日志写入 `~/.postel/logs/`，按大小轮转。
4. 日志中**不得**出现凭据、token、绝对路径以外的敏感信息。
5. `doctor` 不修改任何配置与状态（只读）。

## 涉及文件

- `src/postel_studio/diagnostics.py`（新建）
- `src/postel_studio/logging.py`（新建：轮转与脱敏）
- `src/postel_studio/__init__.py`（增加 `doctor` 子命令）
- `tests/test_doctor.py`（新建）

## 测试要求

- 断言输出含全部检查项。
- 脱敏测试：构造含 `ghp_` / `pypi-` 的日志行，断言被替换。
- 断言 `doctor` 前后 `state.db` 与配置未变。

## 禁止事项

- 不得在 `doctor` 中执行会改动状态的命令。
- 不得把凭据原样写入日志。
- 不得让 `doctor` 依赖网络（离线可用）。
