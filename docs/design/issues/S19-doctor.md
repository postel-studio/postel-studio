# S19 · 一条命令自检

- **里程碑** M3 · **预计** 3h · **依赖** S1 · **优先级** P2
- **用户路径**：跑 `postel-studio doctor` → 看到内核/端口/DB/git/Agent 各项 ✓ 或 ✗ 与修复建议

## 背景

常驻进程出问题时，用户不该翻源码。这一刀给一个**只读**的自检出口。

## 验收标准

1. 输出：内核是否存活、端口、`POSTEL_HOME`、SQLite 可写、git 可用、Agent 探测结果。
2. 每项给出 `ok / warn / fail` 与一句修复建议。
3. 日志写入 `~/.postel/logs/`，按大小轮转。
4. 日志中的 token 被脱敏（含长度泛化）。
5. `doctor` **只读**：运行前后 `state.db` 与配置均不变。
6. 离线可用（不依赖网络）。

## 涉及文件

- `src/postel_studio/diagnostics.py`（新建）
- `src/postel_studio/logging.py`（新建，轮转与脱敏）
- `src/postel_studio/__init__.py`（加 `doctor` 子命令）
- `tests/test_doctor.py`（新建）

## 测试要求

- 断言输出含全部检查项。
- 脱敏：构造含 `ghp_` / `pypi-` 的日志行，断言被替换且长度也被隐藏。
- 断言命令前后 `state.db` 的 mtime 与内容哈希不变。

## 禁止事项

- 不得在 `doctor` 中执行会改动状态的命令。
- 不得把凭据原样写入日志。
- 不得依赖网络。
