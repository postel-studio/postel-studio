# I04 · 展示分支与提交的 diff（UI 片段）

- **里程碑**：M1　**预计**：2h　**依赖**：I03　**优先级**：P0

## 背景

「看 diff」是这个产品的核心动作之一，需要在 M1 就证明它可读、不卡。本切片只做单个 diff 视图。

## 验收标准

1. `repo.diff` 接受 `base` / `head` 两个 ref，返回 `stat`（文件数/增/删）与 unified diff 文本。
2. 非法 ref 返回 `error.code == "BAD_REF"`，不是 500。
3. diff 超过上限时返回截断标记，**不静默丢内容**。
4. 前端把 diff 渲染为行号 + 增删底色；`+` 绿、`-` 红，hunk 头单独着色。
5. 用户可以在输入框里填任意两个 ref 进行对比（不限于分支）。

## 涉及文件

- `src/postel_studio/gitops/repo.py`（增加 `diff_stat` / `diff_text`）
- `src/postel_studio/api/app.py`（增加 `repo.diff`）
- `src/postel_studio/web/assets.py`（diff 渲染片段）
- `tests/test_diff.py`（新建）

## 测试要求

- 真实仓库的 diff 统计精确断言（文件数/增/删三个数都要对）。
- 截断路径必须有测试（小上限触发）。
- 非法 ref 的错误码测试。

## 禁止事项

- 不得用 `A or B` 形式的松散断言（那等于没断言）。
- 不得渲染 HTML 原始内容（必须转义）。
- 不得在前端直接调用 git。
