# S8 · 点「新建 Bot」→ 左栏出现它

- **里程碑** M2 · **预计** 3h · **依赖** S6 · **优先级** P0
- **用户路径**：点「新建 Bot」→ 填名字与权限 → 左栏出现该 Bot → 刷新后仍在

## 背景

S6 的 Bot 是预置示例。真实使用时人要能自己建，并**当场选择权限**——这是权限模型第一次对人可见。

## 验收标准

1. 输入合法名字与权限级别，创建成功并出现在左栏。
2. 名字非法（大写/空格/超长）显示可读错误，不创建。
3. 权限下拉**只出现** L0/L1/L2/L2+，不出现 L3/L4。
4. 默认权限为 L1（可改工作区，不可推送）。
5. 创建时同步建立记忆目录，charter 可从模板生成。
6. 刷新后仍在。

## 涉及文件

- `src/postel_studio/domain/models.py`（加 `Permission` 枚举与判定）
- `src/postel_studio/storage/db.py`（加 `bots` 表）
- `src/postel_studio/api/app.py`（加 `bot.create` / `bot.set_permission`）
- `src/postel_studio/web/assets.py`（新建 Bot 表单）
- `tests/test_bot_create.py`（新建）

## 测试要求

- 表驱动：合法名 × 非法名 各若干，断言创建/拒绝。
- 断言 L3/L4 尝试设置返回 `NOT_SUPPORTED`。
- 断言默认权限 = L1。
- 权限包含关系单测（持 L2 具备 L1）。

## 禁止事项

- 不得把 L3/L4 放进选项列表（即使灰掉也会误导）。
- 不得允许 Bot 自己改权限（本刀只对人的入口）。
- 不得合并 L3 与 L4 为一级。
