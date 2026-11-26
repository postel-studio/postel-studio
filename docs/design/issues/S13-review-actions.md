# S13 · 批准 / 拒绝 → 状态变化

- **里程碑** M2 · **预计** 3h · **依赖** S11,S12 · **优先级** P1
- **用户路径**：点候选 → 点「批准」或「拒绝并说明」→ 条目状态变化并从默认列表消失/保留

## 背景

收件箱是**人的验收台**。这一刀让"人做决定"真正生效——这是整个产品最核心的人机边界。

## 验收标准

1. 批准后状态变 `approved`，UI 立即反映。
2. 拒绝后状态变 `rejected`，从默认列表消失（筛选「已拒绝」仍可见）。
3. 拒绝可填理由，理由被持久化。
4. **无「合并」按钮**（任何形式）。
5. 权限不足的操作按钮禁用并显示原因。

## 涉及文件

- `src/postel_studio/domain/models.py`（`ReviewState` 状态机）
- `src/postel_studio/api/app.py`（加 `candidate.approve` / `candidate.reject`）
- `src/postel_studio/web/assets.py`（操作按钮区）
- `tests/test_review.py`（新建）

## 测试要求

- 状态机：`local_only → approved` 合法，`rejected → approved` 非法（断言被拒）。
- 断言 UI 按钮集合**不含**「合并」。
- 权限不足时按钮 `disabled` 且有原因文案。

## 禁止事项

- 不得提供合并入口。
- 不得在拒绝时删除候选分支（保留可追查）。
- 不得静默失败（按钮点了没反应）。
