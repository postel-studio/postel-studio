# 代码地图（Code Map）

> 状态：S2 完成后建立 · 2026-11-26
> 维护约定：**每个切片合并前更新本目录**，由 `scripts/gate-codemap.sh` 强制检查（见 §维护规则）。

---

## 这份文档是给谁看的

给**下一个要改这份代码的人或 Agent**看。它回答四个问题：

1. **这个能力在哪个文件里？**（§04 按能力索引）
2. **这个文件为什么长这样？**（§03 逐模块，含「为什么这么写」与被推翻的方案）
3. **我改这里，还会波及什么？**（§05 变更影响矩阵）
4. **哪些地方看起来可以改、其实改了会坏？**（§06 陷阱清单）

**它不是设计文档。** 设计（为什么要有这个产品、协议怎么定、权限怎么切）在
[`docs/design/`](../design/README.md)。**也不是 API 文档**——这里的每个条目都指向源码，
源码的 docstring 才是最新事实，本文负责**索引与因果**。

> 一句话：设计文档说「应该是什么」，代码地图说「现在实际是什么、以及为什么和当初想的不一样」。

## 阅读顺序（自上而下）

```
README.md            ← 你在读的索引页；先看这一页决定往下看哪几节
   │
   ├─ 01-overview.md      系统全景：进程/线程/连接、一次请求的完整路径
   ├─ 02-layers.md        分层与依赖规则：谁能 import 谁（含可执行检查）
   ├─ 03-modules/         逐模块：职责、关键决策、被推翻的方案
   │     ├─ entry.md          └ __init__.py（CLI 入口）
   │     ├─ server.md         └ api/server.py（生命周期/信号/日志）
   │     ├─ app.md            └ api/app.py（路由表 + 装配）
   │     ├─ gateway.md        └ api/gateway.py（WS 网关）
   │     ├─ repository.md     └ api/repository.py（repo.* 业务编排）
   │     ├─ frames.md         └ api/frames.py（帧编解码）
   │     ├─ domain.md         └ domain/models.py（Repo 实体/repo_id）
   │     ├─ gitops.md         └ gitops/repo.py（git 子进程）
   │     ├─ storage-db.md     └ storage/db.py（state.db）
   │     ├─ paths.md          └ storage/paths.py（状态根/文件 IO）
   │     └─ assets.md         └ web/assets.py（页面与前端逻辑）
   ├─ 04-capabilities.md  按用户可见能力反查代码位置
   ├─ 05-change-impact.md 改动影响矩阵：改 X 要连带检查 Y
   └─ 06-pitfalls.md      陷阱清单：看起来能改、改了会坏的地方
```

**按目的跳转：**

| 我想… | 去 |
|---|---|
| 快速搞清「一个请求怎么走到头」 | [01-overview](01-overview.md) |
| 加一个新 API 方法 | [04 §加方法](04-capabilities.md#加一个新方法ws-帧) → [05 §改协议](05-change-impact.md) |
| 加一个新前端能力 | [03/assets](03-modules/assets.md) → [06 §前端](06-pitfalls.md#前端) |
| 修「内核起不来」类问题 | [03/server](03-modules/server.md) → [06 §启动](06-pitfalls.md#启动与退出) |
| 碰安全相关（令牌/路径/日志） | [05 §安全红线](05-change-impact.md#安全红线改动涉及以下任一必须逐条过) → [06 §安全](06-pitfalls.md#安全) |
| 接手某个 Issue 前 | [04](04-capabilities.md) + 对应 `docs/design/issues/S<N>.md` |

## 当前覆盖范围（诚实声明）

| 区域 | 状态 | 说明 |
|---|---|---|
| `__init__.py` CLI | ✅ 完整 | |
| `api/server.py` | ✅ 完整 | 含信号竞态、日志可见性两处非显然逻辑 |
| `api/app.py` / `api/gateway.py` / `api/frames.py` | ✅ 完整 | |
| `storage/paths.py` | ✅ 完整 | |
| `storage/db.py` | ✅ 完整 | S3 新增；只有一张表，迁移机制已立 |
| `gitops/` | ✅ 完整 | S3 新增；只读探测 + 子进程外壳 |
| `domain/` | ✅ 完整 | S3 新增；无 IO，纯函数 |
| `api/repository.py` | ✅ 完整 | S3 新增；两个方法 |
| `web/assets.py` | ⚠️ 部分 | JS 状态机有注释索引；**DOM 行为无自动化测试**，见 06 §前端（S3 的左栏同样只靠文本断言 + 人工浏览器） |
| `tests/` | ⚠️ 部分 | 只索引「哪个测试守哪条约束」，不复述用例 |
| `scripts/check.sh` | ✅ 简短 | 门禁入口，规则见 [05 §门禁](05-change-impact.md#门禁) |
| `scripts/gate-codemap.sh` + `scripts/hooks/pre-commit` | ✅ 简短 | 本目录的**强制执行者**；改它必须同时跑 `tests/test_gate_codemap.py` |
| **尚未存在**：调度、ACP、worktree、权限判定、记忆注入、订阅 | ⛔ 无 | SQLite / git 已在 S3 出现；**新模块必须同时新增本目录对应条目** |

## 维护规则（合并前的硬要求）

**这条是门禁，不是建议。** `bash scripts/check.sh codemap`（`scripts/gate-codemap.sh`）
会比对相对 `main` 的改动：**`src/` 或 `tests/` 有改动而本目录零改动时，门禁直接失败**。
本地 `pre-commit` 钩子也跑这一项，CI 同样跑。

> 为什么这么严：这个仓库的代码主要由 Agent 产出、人类审核。没有地图，下一个人
> （或下一个 Agent）只能通读源码才能判断改动，diff 再小也失去意义。

| 你动了什么 | 必须更新 |
|---|---|
| 新增模块 / 新增文件 | 新增 `03-modules/<name>.md`，并在 §阅读顺序的树里挂上 |
| 新增/删除/重命名公开符号 | 该模块页的「符号表」**行号** |
| 改变跨模块调用关系 | `02-layers.md` 的允许边表；违例会被 §02 的可执行检查抓住 |
| 新增路由 / 帧类型 / 错误码 | `01-overview.md` 的请求路径 + `04-capabilities.md` 对应条目 |
| 改安全相关行为（令牌/路径/日志/权限） | 逐条过 `05-change-impact.md` 的安全红线 |
| 修掉一个陷阱 | `06-pitfalls.md` 标「已修 + 怎么修的」，**不要直接删**（下一个人需要知道这里踩过坑） |
| 一条「看起来能改但改了会坏」的事实 | `06-pitfalls.md` 新增一条，这是本目录最大的价值 |
| 纯格式化 / 纯注释错字 | 不需要动地图，PR 描述写一行 `codemap: N/A - <原因>` |

**行号是本目录最脆弱的部分。** `docs` 门禁（S4.5，`scripts/gate-docs.py`）能自动查的是
**markdown 链接里的行号表达式**（`…md#L24-L30` 越界）与**模块行数表**；
而 `03-modules/*.md` 里大量 `文件:行号` 是**反引号文本、不是链接**，仍然只能手工核对。
挪动一个被索引的符号就得改那个数字——行号错了，地图就在撒谎，而撒谎的地图比没有地图更危险。
把 `文件:行号` 写成 markdown 链接（`[…](…py#L24)`）就能被门禁覆盖，是新写的首选形式。

本目录变更可随 slice 一起提交，**不单独占 PR**（它服务于同一刀的代码）。

> 判断标准：如果一个问题让你花了超过 15 分钟才搞明白，它就该进 `06-pitfalls.md`。

## 与其它文档的关系

| 文档 | 职责 | 何时看 |
|---|---|---|
| [docs/design/](../design/README.md) | 设计决策、需求、协议契约 | 想知道「为什么做这个」 |
| **docs/codemap/（本文）** | 代码现状、依赖、影响面、陷阱 | 想知道「怎么改、改哪里」 |
| `docs/design/04-task-breakdown.md` | 切片计划 S1~S20 | 想知道「下一步做什么」 |
| 各模块 docstring | 该文件的一手事实 | 永远以此为准 |
| [CONTRIBUTING.md](../../CONTRIBUTING.md) | 门禁、提交、PR 流程 | 提交前 |

**冲突时的优先级**：源码 docstring > 本目录 > 设计文档。
若发现本目录与源码不符，**以源码为准并修本目录**——文档漂移比没文档更危险。