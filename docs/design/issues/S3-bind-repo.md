# S3 · 填路径 → 左栏出现这个仓库

- **里程碑** M1 · **预计** 3h · **依赖** S2 · **优先级** P0
- **用户路径**：在输入框粘贴仓库路径 → 点「绑定」→ 左栏出现仓库行（名字 + 默认分支）→ 刷新页面仍在

## 背景

第一刀真正碰业务：把"本地有个仓库"变成"工作台认得它"。这一步打通 数据 → 接口 → UI 三层的**最小**形态。

**这是第一次出现「内核持有用户数据」**：第一次有 SQLite、第一次起子进程调 `git`、第一次让用户输入
决定文件系统访问。05-security §7 的验收清单从这一刀起**每一刀都要过**。

---

## 验收标准

1. 输入一个真实 git 仓库路径，绑定后左栏出现该仓库（显示名称与默认分支）。
2. 输入非 git 目录 → 显示可读错误「不是 git 仓库」，不崩溃、不留半状态。
3. 刷新页面后仓库仍在（已持久化）。
4. 同一路径重复绑定 → 不产生重复行。
5. 绑定信息落到 `~/.postel/state.db`（或等价的落盘），不写进仓库。

### 验收的**操作级**口径（人可以照着做）

```bash
# 准备：真实仓库（裸 git init 的目录也算，见 D2）
mkdir -p /tmp/demo && cd /tmp/demo && git init -q && git commit -q --allow-empty -m init

POSTEL_HOME=/tmp/ps-demo uv run postel-studio serve
# → 浏览器打开打印出来的地址
```

1. 左栏仓库区有输入框，粘贴 `/tmp/demo`，点「绑定」→ 左栏出现 `demo`（默认分支 `main`）。
2. 再粘 `/tmp`（非仓库）→ 出现红色可读错误「不是 git 仓库」，左栏列表不变。
3. 刷新页面 → `demo` 仍在。
4. 再绑一次 `/tmp/demo` → 仍然只有一行。
5. `python3 -c "import sqlite3;print(sqlite3.connect('/tmp/ps-demo/state.db').execute('select path from repos').fetchall())"`
   → 打印出 `/tmp/demo`；同时 `git -C /tmp/demo status` 与绑定前无差异（没往仓库里写东西）。

---

## 设计裁决（D1–D6）

本刀第一次碰业务，设计文档没覆盖到的地方必须在这里裁决，**不接受实施者自行发挥**。

### D1 · 端口：方法走 WebSocket，HTTP 只留 S1/S2 已有的三条

| 裁决 | 理由 |
|---|---|
| `repo.bind` / `repo.list` 走 **WS 帧** | 07 §2 的方法清单是「帧方法」，02b 定义了 req/res 包络；HTTP 只服务页面与 `where`/`health`。**不得**顺手加 `/api/repos`——那会长出第二套 API 面。 |

**这条有可观测后果**：若内核不在（连接断了），左栏就没有仓库列表。UI 必须显示「已断开」而不是
空列表——**「空」和「没连上」必须可区分**（这是 S18 的主题，但 S3 起就得做对，否则是撒谎）。

### D2 · 裸仓库与子目录：接受，但**以真实根为准**

- `git init` 出来的**没有提交**的仓库：`is_repo` 为真，`default_branch` 为 `main`（git 初始化的缺省值）。
  **不要求有提交**——用户刚建仓就想绑，是正常诉求。
- **裸仓库**（`git init --bare`）：接受。`default_branch` 取 `HEAD` 指向，可能为 `null`。
- 用户可能粘贴**子目录**（如 `/home/u/proj/src`）：**必须绑定到仓库根**，不绑子目录。
  绑定后左栏显示的是根目录名字。**这一点必须在验收里断言**，因为它是「用户以为绑了 A 其实绑了 B」的坑。

### D3 · `repo_id`：按设计文档算，但**无 origin 时用路径**——这是有意的让步

06 §2.1 写的是 `sha256(归一化 origin URL)[:12]`，「无 origin 时用绝对路径」。
本刀**照做**，而且要断言：同一仓库的 `https://github.com/o/p.git` 与 `git@github.com:o/p.git`
归一化后哈希相同。`repo_id` 的长度**必须**是 12（07 §4.1 的示例 `adec6540a725` 就是 12 位）。

> 已知不完美：加了 origin 的仓库，`repo_id` 会变（从路径哈希变成 URL 哈希）。本刀**不做**迁移，
> 但要记进 codemap 的未决问题——S4 之前必须解决，否则用户加了 remote 之后仓库会"变一个"。

### D4 · 路径校验：允许根 = **用户显式输入的绝对路径**

05 §3.2 说「所有用户输入的路径必须 `resolve()` 后断言其在允许根之内」，允许根写的是
「绑定的仓库路径、worktrees、bots」。**对绑定这个动作本身，这句话是循环的**——绑定的输入就是允许根本身。

裁决：
- `path` 必须是**绝对路径**；相对路径 → `BAD_CWD`（07 §1.3 已有此码，别新造）。
- 必须 `Path.resolve()`（消 `..` 与符号链接）后再用。
- 不存在 / 不是目录 → `NOT_A_REPO`（不新造码）。
- 绑定**之后**的所有操作（S4 的分支列表等）必须以**已绑定的 `path`** 为允许根，
  这是 05 §3.2 真正生效的时刻。本刀只需在 `gitops` 里把「接受一个根」做成显式参数。

### D5 · 子进程：字面数组 + `GIT_TERMINAL_PROMPT=0` + 超时

- `subprocess.run([...], shell=False, capture_output=True, text=True, timeout=5)`，
  **禁止** `shell=True` / `os.system` / 字符串拼接（05 §3.3）。
- 环境变量必须含 `GIT_TERMINAL_PROMPT=0`（禁止交互式提示，否则内核会挂住）；
  同时 `GIT_CONFIG_NOSYSTEM=1`，避免用户机器的全局 git 配置影响结果。
- **超时必须有**。git 卡住（坏 repo、锁文件、网络 fs）会让整个内核无响应。
- `git` 不存在 → `ADAPTER_UNAVAILABLE`（07 §1.3 有此码，语义是"命令不存在"）。
  **不崩内核**。

### D6 · 错误码：只用已登记的，**一个都不新造**

本刀用到的全部来自 07 §1.3 现表：

| 场景 | 码 |
|---|---|
| 路径不是绝对路径 | `BAD_CWD` |
| 路径不存在 / 非目录 / 不是 git 仓库 | `NOT_A_REPO` |
| `path` 参数缺失或不是字符串 | `INVALID_PARAMS` |
| `git` 命令不存在 / 起不来 | `ADAPTER_UNAVAILABLE` |
| `git` 超时 | `INTERNAL`（**不**新造 `GIT_TIMEOUT`；先按 INTERNAL 处理，S19 doctor 再细化） |
| `repoId` 不存在 | `REPO_NOT_BOUND` |

**`NOT_A_REPO` 的 message 不得回显解析后的绝对路径**（05 §4.2：错误不回显输入值，只回码与字段名）。
这条**必须有测试**——它是最容易顺手写错的地方。

---

## 涉及文件（预计 11 个）

| 文件 | 内容 | 预估行数 |
|---|---|---|
| `src/postel_studio/gitops/__init__.py` | 空包标记 | 3 |
| `src/postel_studio/gitops/repo.py` | `is_repo` / `origin_url` / `default_branch` / `repo_root`；子进程外壳 | ~90 |
| `src/postel_studio/storage/db.py` | `state.db` 连接、schema v1（仅 `repos`）、`user_version` 迁移 | ~90 |
| `src/postel_studio/domain/__init__.py` | 空包标记 | 3 |
| `src/postel_studio/domain/models.py` | `Repo` dataclass、`repo_id()` 归一化 | ~45 |
| `src/postel_studio/api/repository.py` | `bind` / `list` 的业务编排（不碰 fastapi） | ~60 |
| `src/postel_studio/api/app.py` | 构造 store + gitops，注入给网关 | ~15 |
| `src/postel_studio/api/gateway.py` | `dispatch` 加两个方法 | ~20 |
| `src/postel_studio/web/assets.py` | 左栏仓库区（输入框 + 列表 + 错误） | ~110 |
| `tests/test_bind.py` | 真实 git 仓库，覆盖验收 1–5 | ~150 |
| `docs/codemap/*` | 按门禁更新（新模块 3 页 + 层图 + 陷阱） | — |

> **行数会超过 400 / 12 文件预算。** 这是本刀第一次触碰 domain + storage + gitops + api + web 五层，
> 不可能更小。作为管理者我的判断：**拆成两刀反而更大**（第二刀要重读第一刀的接口）。
> 按你的裁决记为「骨架例外」，但**新增公开接口控制在 3 个以内**（`repo.bind` / `repo.list` / `RepoStore`）。

---

## 测试要求

**不得 mock git**——用真实临时仓库（`git init` + 一次提交）。

| 用例 | 断言 |
|---|---|
| 绑定普通仓库 | 返回的 `repoId` 是 12 位十六进制；`name` 是目录名；`defaultBranch` 是 `main` |
| 绑定**子目录** | 落库的 `path` 是**仓库根**，不是子目录（D2） |
| 绑**裸仓库** | 成功；`defaultBranch` 允许为 `null` |
| 绑**空仓库**（无提交） | 成功（D2） |
| 绑非 git 目录 | `NOT_A_REPO`；**message 不含该路径**（D6） |
| 绑相对路径 | `BAD_CWD` |
| 绑不存在的路径 | `NOT_A_REPO`（不崩） |
| 重复绑定 | 第二次返回**同一个** `repoId`；`repo.list` 只有一条 |
| 持久化 | 关掉 store 再开，列表还在（真 SQLite 文件，不是内存库） |
| origin 归一化 | `https://github.com/o/p.git` 与 `git@github.com:o/p.git` 得到同一 `repo_id` |
| 无 origin | 用绝对路径算，12 位 |
| `git` 不存在 | `ADAPTER_UNAVAILABLE`（把 PATH 设空） |
| 不往仓库里写 | 绑定前后 `git status --porcelain` 一致 |
| WS 端到端 | 真实 WebSocket 连上 → `repo.bind` → `repo.list` 拿回它 |

## 禁止事项

- 不得只写接口不管 UI（那样演示不了）。
- 不得在 git 调用中允许交互式提示（必须 `GIT_TERMINAL_PROMPT=0`）。
- 不得解绑时删除用户仓库里的任何文件。
- 本刀**不做**分支列表 —— 那是 S4，不要顺手写。
- 不得新造错误码（D6）。
- 不得把 `repo.db`/`state.db` 写进用户仓库（只能写 `~/.postel/`）。
- 不得用 `shell=True`、不得字符串拼接 git 命令。
- 不得在错误 message 或日志里回显解析后的用户路径（05 §4.2）。
- 不得让左栏在断连时显示空列表——「没连上」与「没有仓库」必须可区分（D1）。

---

## 完成定义（DoD）

- [ ] 上面 5 条验收标准逐条在**真实浏览器**里做到（截图或录屏为证）
- [ ] 测试要求表全部实现，且**反向验证**过（把关键实现删掉，测试变红）
- [ ] `bash scripts/check.sh` 全绿
- [ ] 05 §7 验收清单逐条答「是/不适用 + 为什么」
- [ ] codemap 更新：新增 `03-modules/gitops.md`、`storage-db.md`、`domain.md`；
      `02-layers.md` 加新的层与边；`06-pitfalls.md` 记本刀踩到的坑
- [ ] 不新增依赖（**SQLite 用标准库 `sqlite3`**，不引入 SQLAlchemy）