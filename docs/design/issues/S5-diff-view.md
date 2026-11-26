# S5 · 点分支 → 看到 diff

- **里程碑** M1 · **预计** 2h · **依赖** S4 · **优先级** P0
- **用户路径**：点某个非当前分支 → 主区显示它与默认分支的差异（统计 + 着色 diff）

## 背景

这是产品的核心动作之一——「看 diff」。M1 就要证明它可读、不卡、不丢内容。

## 验收标准

1. 点击非默认分支，显示 `默认分支 → 该分支` 的 `N 个文件 · +X −Y` 统计。
2. diff 文本按行着色：新增绿、删除红、hunk 头单独色。
3. 支持手动填两个 ref 进行对比（不限于分支）。
4. 内容超过上限时返回 `truncated: true` 并在 UI 显示「已截断」，**不静默丢**。
5. 非法 ref 显示可读错误，非 500。

## 涉及文件

- `src/postel_studio/gitops/repo.py`（加 `diff_stat` / `diff_text`）
- `src/postel_studio/api/app.py`（加 `repo.diff`）
- `src/postel_studio/web/assets.py`（diff 渲染）
- `tests/test_diff.py`（新建）

## 测试要求

- 真实仓库的 `stat` 精确断言（文件数/增/删三个数都要对，**禁止 `a or b` 式断言**）。
- 截断路径测试：设小上限触发。
- 非法 ref → `BAD_REF`。
- XSS 断言：文件名含 `<script>` 时必须被转义。

## 禁止事项

- 不得在前端直接调用 git。
- 不得渲染未经转义的 HTML。
- 不得用固定宽度假设中文（中文字符宽度不同，需测）。
