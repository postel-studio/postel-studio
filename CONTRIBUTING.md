# 贡献指南

## 质量门禁（唯一标准）

本地与 CI 跑同一份脚本，**不允许分叉**：

```bash
bash scripts/check.sh          # 全部
bash scripts/check.sh lint     # 单项：lint | format | types | test | docs | layers | codemap | secrets
bash scripts/check.sh build    # 打包冒烟（不在 all 里：慢，且需要 dist/）
```

| 门禁 | 工具 | 失败即阻断 |
|---|---|---|
| lint | `ruff check` | 是 |
| format | `ruff format --check` | 是 |
| types | `mypy --strict` | 是 |
| test | `pytest --cov`，覆盖率下限 60% | 是 |
| docs | `scripts/gate-docs.py`：相对链接 / 锚点 / `#L` 行号 / 模块行数表 / 页面 id 一致性 | 是 |
| layers | `scripts/gate-layers.py`：只许向下 `import`，新模块必须在 LAYERS 里登记 | 是 |
| codemap | `scripts/gate-codemap.sh`：动了 `src/`、`tests/` 就必须动 `docs/codemap/` | 是 |
| secrets | 真实凭据形态扫描 + 与 `.env` 逐值比对 | 是 |
| build | `uv build` + 装进干净虚拟环境跑 `postel-studio version` | 仅 CI + 手动 |

- `pre-commit` 钩子只跑快的几项（lint / format / docs / layers / codemap / secrets），完整门禁留给 CI。
- 配置全部在 `pyproject.toml`，CI 用 `uv sync --frozen` 保证依赖可复现。

## 覆盖率

当前下限 **60%**（M1 起步）。随 Issue 推进逐步上调；**任何新模块必须自带测试**，不允许先合并空实现。

## 测试约定

- 真实行为优先：git / ACP / worktree 必须用真实临时仓库或真实子进程，**不得只用 mock**。
- 需要真实 Agent（opencode / dsh / pi）的用例用 `@pytest.mark.real_agent`，缺失时 skip 而非失败。
- 慢集成测试用 `@pytest.mark.slow`。
- 断言要精确：禁止 `assert a or b` 这类等价于没断言的写法。
- **反向验证**：给一条安全/契约约束加测试时，顺手在 `tests/test_reverse_checks.py` 的
  `INJECTIONS` 里加一行"注入这个缺陷 → 这条测试必须红"。原因见
  [06-pitfalls T23/T30](docs/codemap/06-pitfalls.md)：**没被反向验证过的测试可能只是装饰品**
  （S3 就抓到一条：把超时改成 0 也还是绿的）。

## 浏览器验收（不在门禁里，改了前端就得手动跑）

前端 DOM 行为是 pytest 覆盖不到的那一块（[06 T9](docs/codemap/06-pitfalls.md)）。改动页面后：

```bash
node tools/acceptance/s3_bind.mjs      # 真 Chrome + 真内核 + 读真 DOM
node tools/acceptance/s4_branches.mjs
node tools/acceptance/s5_diff.mjs
node tools/acceptance/s6_bots.mjs
```

它**不进依赖、不跑 CI**，缺 Chrome / puppeteer-core 时会打印 `SKIP …` 并以 0 退出。
用法与覆盖范围见 [`tools/acceptance/README.md`](tools/acceptance/README.md)。

M1 的全链路验收（真内核 + 真 git + 真 WS 帧 + isolated `POSTEL_HOME`）有单独一条命令，
改了后端/协议也要跑：

```bash
uv run python scripts/accept_m1.py    # 必须 exit 0 且打印 M1 OK（6/6 步 + 家具）
```

## 代码地图（改代码前先看，改完代码必须更新）

[`docs/codemap/`](docs/codemap/) 是代码的自上而下索引：每个模块为什么长这样、改一处还要连带
检查什么、哪些地方看起来能改其实改了会坏。它**不是**设计文档——`docs/design/` 说「应该是什么」，
代码地图说「现在实际是什么」。

> **这条是门禁，不是建议。** 本仓库是 Agent 产出的代码为主、人类审查为辅的工作方式：
> 没有地图，下一个人（或下一个 Agent）只能靠读全部源码来理解改动，diff 小也没用。
> 所以 `src/`、`tests/` 有任何改动而 `docs/codemap/` 零改动时，**`check.sh` 与 CI 直接失败**。

命令：`bash scripts/check.sh codemap`（相对 `main` 比对；含未提交改动）。

**每次改代码，按下表对照一遍**（详见 [codemap §维护规则](docs/codemap/README.md#维护规则合并前的硬要求)）：

| 你动了什么 | 必须更新 |
|---|---|
| 新增模块 / 新增文件 | 新增 `docs/codemap/03-modules/<name>.md`，并挂进 `README.md` 的阅读顺序树 |
| 新增/删除/重命名公开函数、类、常量 | 该模块页的「符号表」**行号**（行号会漂，见下） |
| 改变跨模块调用关系 | `02-layers.md` 的允许边表；违例会被 §02 的可执行检查抓住 |
| 新增 HTTP 路由 / WS 帧类型 / 错误码 | `01-overview.md` 的请求路径 + `04-capabilities.md` 对应条目 |
| 改动令牌、路径、日志、权限相关行为 | 逐条过 `05-change-impact.md` 的**安全红线** |
| 修掉一个陷阱 | `06-pitfalls.md` 标「已修 + 怎么修的」，**不要删条目** |
| 遇到花 >15 分钟才搞明白的事 | `06-pitfalls.md` 新增一条 |
| 只是格式化 / 改注释错字 | 不需要动地图，但 PR 描述里写一行 `codemap: N/A - <原因>` |

**行号是 Map 里最容易腐坏的部分。** 行号一变，模块页就撒谎；撒谎的地图比没有地图更危险。
所以改代码后如果挪动了某个被索引的符号，顺手改掉那个数字——`docs/codemap/` 里所有
`文件:行号` 都是这么来的，没有自动化工具，这是人工约定。

> 判断标准：如果你改完之后，地图里描述的「怎么改、改哪里」已经不对了，那一刀就没做完。

## 提交约定

- 提交前 `scripts/check.sh` 必须全绿（含 codemap 门禁）。
- **不得**提交 `.env` 或任何凭据；已有门禁与钩子拦截。
- 一个 Issue 一个提交，提交信息引用 Issue 编号。

## 分支与 PR 流程

**不直接推 `main`。** 每个改动走特性分支 + PR：

```bash
git checkout -b <type>/<short-slug>     # docs/security-data-api, feat/i01-kernel, fix/...
# ... 改动 ...
bash scripts/check.sh                   # 本地门禁
git commit -m "<type>: <summary>"
git push -u origin <branch>             # 推分支，不推 main
# 用 GitHub API 或网页创建 PR 到 main
```

- PR 必须等 `ci` 工作流通过（`pull_request` 触发，与本地同一份门禁）。
- 合并前自查 `docs/design/05-security.md` §7 的验收清单。
- 分支命名：`type/slug`，`type` ∈ `docs` / `feat` / `fix` / `ci` / `chore`。

> **为什么不用直推**：私有仓库当前**不支持分支保护**（需 GitHub Pro 或转公开）。
> 也就是说 `main` 在服务端没有任何强制约束，唯一能防住误操作的就是这个流程约定本身。
> 转公开或升级计划后应补上分支保护：要求 PR + CI 通过才能合并。

## 开发顺序

见 [docs/design/04-task-breakdown.md](docs/design/04-task-breakdown.md)。按 Issue 编号顺序推进，不做横切。