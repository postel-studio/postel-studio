# 设计原型

> 讨论产物，**不是可交付实现**。实现以 [../issues/](../issues/) 的验收标准为准。

## 怎么看

直接用浏览器打开对应 HTML，**无需构建、无需启动服务**：

| 版本 | 文件 | 状态 |
|---|---|---|
| **v2（当前）** | [`postel-studio-proto-v2.html`](postel-studio-proto-v2.html) | 按真实 GitHub 复刻 7 个一级 tab + Insights 11 子页 |
| v1 | [`postel-studio-proto-v1.html`](postel-studio-proto-v1.html) | 首次全仿 GitHub，含 Inbox / Agents tab |
| v0 | [`v0/postel-studio-proto.html`](v0/postel-studio-proto.html) | 最初骨架，仅用于留档 |

> GitHub 不渲染 HTML 文件本身，只能看源码。要看效果请 clone 后双击打开。

## 版本差异

| | v0 | v1 | v2 |
|---|---|---|---|
| 左栏结构 | 上 Bot / 下仓库，可折叠 | 同 v0，更接近 GitHub 侧栏 | 同 v0 |
| 主区骨架 | 简版 | 仿 GitHub | 仿 GitHub |
| 一级 tab | 无 | Code / 变更收件箱 / Agents / Issues / PRs / Actions / Insights | **Code / Issues / Pull requests / Actions / Projects / Security and quality / Insights** |
| README 区块 | 无 | 无 | **有**（文件表下方） |
| 右侧 About | 无 | 无 | **有**（描述 / Topics / Releases / Packages / Contributors / Languages） |
| 暗色主题 | 有 | 有 | 有 |

**v2 是当前基线**。它与截图中的 7 个 tab 逐字一致，不含 Agents。

## 一个刻意保留的缺口

v2 严格按「7 个 tab」复刻，因此**变更收件箱没有入口**。它是产品的第一视图，但入口位置仍是待定项
（[../03-ui-spec.md](../03-ui-spec.md) §6 Q1）。要看收件箱的设计意图，请打开 v1。

## 截图

完整截图见 [`../shots/`](../shots/)：

- `shots/v2/` — 7 个页面 + Insights 子页 + 暗色
- `shots/v1/` — 含变更收件箱与 Agents 页
- `shots/v0/` — 最初骨架

截图由浏览器实际渲染后抓取。**注意**：抓取时需屏蔽浏览器插件注入的浮层（如翻译插件的
`YD-*` 节点），否则截图里会出现非页面元素的遮挡。