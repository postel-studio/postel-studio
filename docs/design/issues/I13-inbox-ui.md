# I13 · 变更收件箱 UI

- **里程碑**：M2　**预计**：4h　**依赖**：I12　**优先级**：P1

## 背景

收件箱是人的主操作界面（D13）。但它**不是** GitHub 的 Pull requests，必须在视觉与文案上与仿制区区分开。

## 验收标准

1. 左列候选列表显示：标题、状态标签（仅本地/已推远端/PR 待审）、Bot 名、`+/−`、相对时间。
2. 右列展示选中候选的 diff（复用 I04 的渲染件）。
3. 按钮顺序固定：批准 → 推送到 origin → 开 PR → 拒绝并说明。
4. **没有「合并」按钮**（L4 永不开放）。
5. 「开 PR」在权限不足时禁用并给出原因文案，不是静默失败。
6. 空态显示「暂无待审变更」，而不是空白。

## 涉及文件

- `src/postel_studio/web/assets.py`（收件箱面板）
- `src/postel_studio/api/app.py`（`candidate.approve/reject/push`）
- `tests/test_inbox_ui.py`（新建，断言 DOM 结构而非截图）

## 测试要求

- 断言按钮集合中**不包含**「合并」。
- 断言权限不足时按钮 `disabled` 且有原因。
- 空态文案断言。

## 禁止事项

- 不得提供合并入口（任何形式）。
- 不得把收件箱文案写成「Pull requests」（会被误解为 GitHub 的 PR）。
- 不得在 UI 层直接判定权限（必须由内核 `permission.check` 决定）。
