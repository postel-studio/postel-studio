# S4 · 点仓库 → 看到分支列表

- **里程碑** M1 · **预计** 2h · **依赖** S3（含 S3.5）· **优先级** P0
- **用户路径**：点左栏仓库 → 右侧出现分支列表 → 当前分支有标记

## 背景

仓库能认了，下一步要能看见"里面有什么"。分支是第一层结构，也是后面派活时选基础分支的前置。

## 验收标准

1. 点击仓库行后，主区列出该仓库所有本地分支。
2. 当前分支有明确标记（如「当前」徽章）。
3. 显示每个分支的短 sha（8 位）与 upstream（若有）。
4. 仓库有 0 个分支（空仓库）时显示空态文案，不是空白。
5. 切换选中仓库，列表随之刷新。

## 涉及文件

- `src/postel_studio/gitops/repo.py`（加 `list_branches`，用 `for-each-ref` 字段分隔输出）
- `src/postel_studio/api/repository.py`（加 `branches`：repoId → 允许根 → gitops）
- `src/postel_studio/api/gateway.py` / `api/app.py`（`repo.branches` 注册与注入）
- `src/postel_studio/web/assets.py`（主区分支面板）
- `tests/test_branches.py`（新建）
- `tools/acceptance/s4_branches.mjs`（新建，真实浏览器）

## 测试要求

- 真实仓库 + 多分支（`main` + `feature`），断言列表内容与当前分支标记。
- 空仓库用例：断言返回空数组而非报错。
- 解析测试：不得依赖人类可读格式（用 `%x00` 分隔字段）。

## 禁止事项

- 不得解析 `git branch` 的人类可读输出（会因颜色/语言变化崩）。
- 不得在分支列表里做 diff 计算（那是 S5）。

---

# 裁决与验收细则（管理层，实施前钉死）

> 本节的每条都是**契约**：实现可以换写法，但下面这些名字、字段、码、DOM id 不许改。
> 上游：[07-api-contract.md](../07-api-contract.md) §2.2 / §4.2、[06-data-model.md](../06-data-model.md) §8、
> [08-repo-id-stability.md](../08-repo-id-stability.md)（`repoId` 语义已稳定，S4 直接消费它）。

## D1 · `branch` 载荷字段（钉死）

`repo.branches` 的 `result.data` 是**数组**，每项四个字段，**不多不少**：

```json
{"name": "main", "sha": "c7a21ad7", "isCurrent": true, "upstream": "origin/main"}
```

| 字段 | 类型 | 规则 |
|---|---|---|
| `name` | string | 去掉 `refs/heads/` 的短名；带 `/` 的（`feat/x`）原样保留 |
| `sha` | string | **8 位**小写十六进制（取 git 给的 40 位 sha 前 8 位） |
| `isCurrent` | boolean | HEAD 指向它才为 `true`；**游离 HEAD 时全部 `false`** |
| `upstream` | string \| null | `%(upstream:short)`；没有 upstream 时是 **`null`**，不是 `""` |

顺序：按 refname 字典序（`for-each-ref` 的缺省顺序），**不**把当前分支提到最前。
理由：稳定顺序才能被测试断言；"当前优先"是展示层的自由，客户端可自行排。

## D2 · 只列本地分支

命令必须是 `for-each-ref refs/heads/ …`。**禁止**列出 `refs/remotes/*`：

- remote-tracking 是**别名**不是分支，混进去会让"共几个分支"与"当前分支"同时失真；
- 实测：不带 `refs/heads/` 时 `origin/main` 会出现在结果里（见 codemap 新坑）。

## D3 · 一次 git 调用取全部字段，`%00` 分隔

```text
git -C <root> for-each-ref refs/heads/ \
    --format=%(refname:lstrip=2)%00%(objectname)%00%(upstream:short)%00%(HEAD)
```

**为什么用 `%(HEAD)` 而不是再跑一次 `symbolic-ref`**：两次调用之间用户可能正在切分支，
"列表"与"当前"就会各说各话；一次调用天然一致。

**必须记住的陷阱（T33）**：`%(HEAD)` 对**非当前**分支输出的不是空串，而是**一个空格**；
而 `_stdout()` 会对整段输出 `.strip()`，于是**最后一个记录的末尾空格被吃掉**。
所以解析时每个字段都要单独 `.strip()`，当前判定写 `field == "*"`，
**不要**写 `if not field` / `field == ""`——那样除最后一行外都会判反。

## D4 · 分层落点（只向下 import，不改 02-layers）

| 层 | 位置 | 职责 |
|---|---|---|
| L0 | `gitops/repo.py` | `BranchInfo`（frozen dataclass）+ `list_branches(root) -> list[BranchInfo] \| None` |
| L3 | `api/repository.py` | `RepositoryService.branches(params)`：repoId 校验 → 允许根 → gitops → 帧载荷 |
| L3 | `api/gateway.py` | `Providers` 加 `repo_branches`，`dispatch()` 加一行 |
| L3 | `api/app.py` | 注入 `repositories.branches` |
| L4 | `web/assets.py` | 主区分支面板 |

`list_branches` 的三态语义（这是 D5 的基础，不许合并）：

| 返回 | 含义 |
|---|---|
| `list[BranchInfo]`（可能为空） | git 答了；**空列表 = 0 个分支的空仓库**，是成功 |
| `None` | git 说"这不是仓库 / 路径没了"（非零退出码） |
| 抛 `GitUnavailableError` / `GitTimeoutError` | 起不来 / 卡住（同 S3 D5、D6） |

`BranchInfo.sha` 存**完整 40 位**；截 8 位是**边界格式化**，发生在 `repository.py`
（07 §4.2 的"字段名/格式在边界转换"同一条纪律）。这样两层各自可测。

## D5 · 错误码映射（禁止新增码）

| 情形 | 码 | 可重试 |
|---|---|---|
| `repoId` 缺失 / 不是非空字符串 | `INVALID_PARAMS` | 修正后 |
| `repoId` 查不到 | `REPO_NOT_BOUND` | 否 |
| 绑定的路径已不是 git 仓库（目录被删 / `.git` 被删） | `REPO_NOT_BOUND` | 否 |
| `git` 不存在 | `ADAPTER_UNAVAILABLE` | 是 |
| `git` 超时 | `INTERNAL`（同 S3 D6，S19 doctor 再细化） | 是 |
| 空仓库（0 提交） | **成功** `data: []` | — |

"路径已不是仓库"用 `REPO_NOT_BOUND` 而不是 `INTERNAL` 的理由：07 §1.3 没有更贴的码，
`INTERNAL` 会把一个**正常的用户处境**（仓库被删）报成"未预期错误需报障"。
码表不许扩，所以选语义最近的一个；文案写"仓库路径已不存在"。

## D6 · DOM 契约（浏览器验收按这些 id 点）

左栏仓库行（改造 `renderRepos`）：`li[data-repo-id]` 增加 `tabindex="0"`、`role="button"`、
被选中时 `data-selected="true"`；`click` 与 `Enter`/`Space` 都要能选中。

主区新增一节，**位置在 `#content` 之后**（不重排既有布局）：

```text
<section id="branches" data-state="idle" aria-live="polite">
  <h2 id="branches-title">分支</h2>
  <p id="branches-state">从左侧选择一个仓库</p>
  <p id="branches-error" role="alert" hidden></p>
  <ul id="branch-list"></ul>
  <p id="branch-empty" hidden>这个仓库还没有任何分支（空仓库）。</p>
</section>
```

| 元素 | 规则 |
|---|---|
| `#branches[data-state]` | `idle`（未选/断开）/ `loading` / `ready` / `error` |
| `#branches-title` | 未选时 `分支`；选中后 `分支 · <name>`（`textContent`，不拼 HTML） |
| 每行 | `li[data-branch-name]`，内含 `.branch-name` / `.branch-sha`（8 位）/ `.branch-current`（文本「当前」，仅当前分支有） |
| `.branch-upstream` | 有 upstream 才创建；`null` 时**不创建** span（不放占位符、不编造） |
| 空仓库 | `data-state="ready"` + `#branch-empty` 可见 + `#branch-list` 空；**不是** error，**不是**空白 |
| 失败 | `data-state="error"` + `#branches-error` 显示按**错误码**映射的中文（复用 `ERROR_TEXT`） |
| 断连 | 回 `idle`、清空列表、`#branches-state` 写"已断开：分支列表未知"——不许留着旧数据装新鲜 |
| 重连且仍有选中仓库 | 自动重取（与 `loadRepos` 同一条路径） |
| 选中的仓库从列表里消失（解绑/重载） | 回 `idle` 并清空 |

## D7 · 测试矩阵（每条验收至少一条真测试，**不 mock git**）

| # | 用例 | 断言要点 |
|---|---|---|
| 1 | 多分支（`main` + `feature` + `中文分支`） | 名称集合、顺序稳定、sha 是 8 位小写 hex |
| 2 | 当前分支标记 | `main` 的 `isCurrent=True`，其余 `False` |
| 3 | upstream（真 `--bare` remote + `push -u`） | `origin/main` 的 `upstream == "origin/main"`，其它是 `None` |
| 4 | 空仓库（`git init` 无提交） | `data == []`，**不抛** |
| 5 | 游离 HEAD | 仍列出全部分支，且**全部** `isCurrent=False` |
| 6 | 只列本地 | push 之后 `origin/main` 存在，但结果里**没有**它 |
| 7 | 不依赖人类可读输出 | 仓库配 `git config color.ui always` + 非 ASCII 分支名，解析仍正确 |
| 8 | 服务层错误码 | 未知 repoId → `REPO_NOT_BOUND`；缺/非字符串 → `INVALID_PARAMS`；仓库目录被删 → `REPO_NOT_BOUND` |
| 9 | 端到端（真 WS 帧） | `repo.branches` 回 `{"data":[...],"seq":N}`，与直调服务层一致 |
| 10 | gitops 三态 | 非仓库路径 → `None`（不抛） |

**既有测试必须改一处**：`tests/test_gateway.py::test_unknown_method_is_unknown_method_not_subscribe`
的反例列表里有 `repo.branches`——它已实现，必须移出；反例仍需**至少 4 个**未实现方法
（`bot.list` / `task.dispatch` / `agent.list` / `system.subscribe` 够）。

## D8 · 文档与门禁

- 07 §4 补 `repo.branches` 的请求/响应示例与 D1 字段表（新增方法必须登记，§5 的兼容规则）。
- 06 §8「明确不存的东西」加一行：分支列表不落库，每次按需从 git 读（视图实体，不是表）。
- codemap：`03-modules/{gitops,repository,gateway,app,assets}.md`、`03-modules/README.md` 行数、
  `04-capabilities.md`、`05-change-impact.md`、`06-pitfalls.md` 新增 **T33**（`%(HEAD)` 空格 +
  `.strip()` 陷阱）。
- `bash scripts/check.sh all` 全绿；`mypy --strict` 覆盖新增的 dataclass 与 `None` 分支。

## 禁止事项（补充）

- 不加 HTTP 路由、不加错误码、不加依赖（含前端构建链）、不写 git（只读）。
- 不做 remote 分支、不做分支切换、不做 ahead/behind、不缓存分支列表。
- 不把 `#branches` 塞进 `#repos`（左栏是列表，主区是内容）。
- 浏览器验收脚本不得进 `pyproject.toml`、不得进 CI（与 S3 同一条裁决）。
