# I08 · Bot 抽象与权限分级判定

- **里程碑**：M2　**预计**：3h　**依赖**：I05　**优先级**：P0

## 背景

权限是产品安全的地基。L3（跨仓库 PR）与 L4（合并）**必须**是两个独立开关，不能由一个「可开 PR」覆盖。

## 验收标准

1. 权限为包含关系：持 L2 同时具备 L1 与 L0。
2. `permission.check(botName, "push")` 分别返回允许/拒绝，错误码 `PERMISSION_DENIED` 稳定。
3. **默认权限不得包含推送**（新建 Bot 默认 L1 或更低）。
4. `L3_foreign_pr` 与 `L4_merge` 不得出现在 MVP 可选项里；尝试设置返回 `NOT_SUPPORTED`。
5. 权限变更后立即生效（无需重启）。
6. `agent.capabilities` 返回的权限清单只含 MVP 支持的级别。

## 涉及文件

- `src/postel_studio/domain/models.py`（Permission / Bot）
- `src/postel_studio/domain/permission.py`（新建：判定纯函数）
- `src/postel_studio/api/app.py`（`permission.check` / `bot.set_permission`）
- `tests/test_permission.py`（新建）

## 测试要求

- 表驱动测试：所有级别 × 所有需求的组合，逐一断言。
- 断言默认权限 < 推送权限。
- 断言 L3 / L4 不可设置，且错误码稳定。
- 纯函数单测，无需 IO。

## 禁止事项

- 不得把 L3 与 L4 合并成一级。
- 不得把默认权限设为含推送。
- 不得在权限判断里读取 Agent 类型（那会引入耦合）。
