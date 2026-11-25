# I19 · 发布流程：版本号、tag、自动发布

- **里程碑**：M3　**预计**：2h　**依赖**：M2 完成　**优先级**：P2

## 背景

仓库已有 Trusted Publishing 工作流（`0.1.2` 已由 Actions 自动发布验证通过）。本切片把版本推进到真实可用的第一个版本。

## 验收标准

1. `pyproject.toml` 版本号更新，`uv build` 产物元数据正确（MIT、LICENSE 随包）。
2. 打 tag 触发 `publish.yml`，工作流 `success`。
3. PyPI 索引可见新版本，`pip install` 后入口可执行。
4. 发布前工作流内的冒烟步骤通过。
5. 发布后在本仓库 README 更新安装版本说明。

## 涉及文件

- `pyproject.toml`
- `.github/workflows/publish.yml`（已有，不改逻辑）
- `README.md` / `README.zh.md`

## 测试要求

- 本地：`uv build` + 从产物安装并执行入口。
- 远端：发布后检查 PyPI 索引与安装。

## 禁止事项

- 不得复用已发布的版本号（PyPI 不可重传）。
- 不得在发布前跳过 LICENSE 随包检查。
- 不得手工 `twine upload`（必须走 Trusted Publishing）。
