# S20 · M3 验收 + 发版

- **里程碑** M3 · **预计** 2h · **依赖** S15~S19 · **优先级** P2
- **用户路径**：跑一次全量验收 → 全部通过 → 打 tag → 自动发布到 PyPI → 可安装运行

## 背景

M3 的收尾：把可上线性的每一项串成一次可复现的验收，并且真的发一个版本出去。

## 验收标准

1. `scripts/accept_m3.py` 一条命令覆盖：重启恢复、越权拦截、上下文可见、空态错误态、doctor。
2. 全绿后更新版本号，打 tag，`publish.yml` 触发成功。
3. PyPI 索引可见新版本，`pip install` 后入口可执行。
4. 发布前工作流的冒烟步骤通过。
5. README 更新安装版本说明。

## 涉及文件

- `scripts/accept_m3.py`（新建）
- `pyproject.toml`（版本号）
- `README.md` / `README.zh.md`（版本说明）
- `tests/test_accept_m3.py`（新建）

## 测试要求

- 真实执行：五项检查各至少一个断言。
- 发布后核对 PyPI 索引与安装。

## 禁止事项

- 不得复用已发布的版本号（PyPI 不可重传）。
- 不得手工 `twine upload`（必须走 Trusted Publishing）。
- 不得在验收脚本里跳过任一检查。
