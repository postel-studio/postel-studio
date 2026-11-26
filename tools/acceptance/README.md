# tools/acceptance — 真实浏览器验收脚本

> 不是 pytest，**不进项目依赖**，**不参与 CI**。它的存在是为了补 `tests/` 补不了的那一块：
> 前端 DOM 的真实行为（[06-pitfalls T9](../../docs/codemap/06-pitfalls.md)）。

## 为什么需要它

`tests/test_bind.py` 能证明接口行为（真实 git + 真 SQLite + 真 WebSocket），但**不能证明**
「左栏真的出现了仓库行」「断连时真的显示『已断开』而不是空列表」。pytest 里没有浏览器。
S3 实施期间这两条只能靠手工点；手工点 = 不可复现 = 下一个人不会做。所以把它落成脚本。

它**不**替代 pytest：断言都在 pytest 里（可 CI、可反向验证），这里只验"眼睛能看到的那一层"。

## 怎么跑

前置（一次）：

```bash
# 1) 项目本身能跑
uv sync

# 2) 本机有 Chrome（任意一个路径都行，二选一）
google-chrome --version
# 或    POSTEL_CHROME=/path/to/chrome node tools/acceptance/s3_bind.mjs

# 3) 有 puppeteer-core（**不用**装进项目；全局装即可）
npm install -g puppeteer-core          # 或任意能让 node 解析到它的位置
```

运行：

```bash
bash scripts/check.sh                  # 先保证代码是绿的（门禁全过再跑验收）
node tools/acceptance/s3_bind.mjs      # S3：绑定仓库 → 左栏出现
node tools/acceptance/s4_branches.mjs  # S4：点仓库行 → 主区出现分支列表
node tools/acceptance/s5_diff.mjs      # S5：点分支行 → 主区出现统计 + 着色 diff
node tools/acceptance/s6_bots.mjs      # S6：左栏出现 Bot → 点开看 charter 全文与记忆目录
```

脚本自己会做这些事，**不碰你的真实 `~/.postel`**（06-pitfalls T18）：

1. 在系统临时目录建一个独立工作区（`/tmp/postel-s3-acceptance-XXXX`）；
2. 用真实 `git init` + 一次提交造一个 demo 仓库；
3. 用 `POSTEL_HOME=<临时目录>` 起一个真内核（`postel-studio serve --port 0`）；
4. 用 Chrome（headless）打开内核打印的地址，**点真按钮、读真 DOM**；
5. 跑验收 1–4、D1、验收 5 的落盘部分；
6. 退出时收掉内核，把截图、内核日志留在那个临时目录里并在结尾打印路径。

它断言的东西与 S3 验收标准的对应关系：

| 输出行 | 对应 |
|---|---|
| `CHECK 1 绑定后左栏出现仓库` / `显示名称与默认分支` | 验收 1 |
| `CHECK 2 非仓库显示可读错误` / `错误后列表不变` | 验收 2 |
| `CHECK 3 刷新后仍在` | 验收 3 |
| `CHECK 4 重复绑定仍一行` | 验收 4 |
| `CHECK 5 state.db 里是仓库根` / `没往用户仓库里写东西` | 验收 5（`state.db` 位置 + 仓库零改动） |
| `CHECK D1 …`（4 行） | D1：断连时「没连上」与「没有仓库」可区分 |
| 末尾那条 `ticket` 检查 | 05 §4.1（连 05 §7 的第 3 条：新增日志不回显敏感值） |

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 全部通过；**或**环境不具备而跳过（第一行会打印 `SKIP 浏览器验收未运行：<原因>`） |
| `1` | 有断言失败（输出 `CHECK ✗ …`，并保留现场） |

**跳过不是失败**：没装 Chrome、没装 puppeteer-core、或没跑过 `uv sync` 都打印原因并以 0 退出。
这样它可以在任何机器上被"跑一下试试"，而不会在 CI 或别人的机器上制造假红。

## 它**不**覆盖什么（别拿它当地图用）

- 多浏览器 / 多标签 / 不同缩放；
- `localhost:` 打开（[06 U4](../../docs/codemap/06-pitfalls.md)）；
- 内核换端口后的重连路径（S3 没动它，S2 的 `test_gateway.py` 各有覆盖）；
- 视觉/对比度（那要看截图，脚本只读 DOM 状态）。

## S4 那份（`s4_branches.mjs`）

同一个骨架，验「点左栏仓库行 → 主区分支面板出现」：

1. 在临时工作区建**两个真仓库**：一个有 `main` + `feature` 且 `main` 带 upstream
   （真 `git init --bare` remote + `push -u`），一个是 `git init` 之后**不 commit** 的空仓库；
2. 真内核 + 真 Chrome，填路径绑定两个仓库；
3. **点仓库行**（鼠标）→ 断言分支列表出现、`main` 有「当前」徽章、8 位 sha、
   upstream 只在有 upstream 的分支上出现；
4. 切到空仓库 → `data-state=ready` + 空态文案（不是错误、不是空白）；
5. 期间真的 `git branch hotfix`，再切回来 → 列表**刷新**（出现 `hotfix`）；
6. 键盘：`Enter` 选空仓库、`Space` 切回来；
7. CDP 断网 → 面板回 `idle` + 「已断开：分支列表未知」+ 列表清空；恢复网络点「重连」→
   自动重取（选中仓库还在）；
8. **错误态**：删掉已绑定仓库的目录再点那一行 → `repo.branches` 回 `REPO_NOT_BOUND` →
   `data-state="error"` + 按**码**映射的「仓库未绑定或已不存在」（不是绑定表单的「绑定失败」）；
   再点另一个仓库能恢复（错误不是终态）。

截图与内核日志落在 `/tmp/postel-s4-acceptance-XXXX/`（结尾打印路径）。

| 输出行 | 对应 |
|---|---|
| `CHECK 1 …`（分支出现 / 顺序 / 标题） | S4 验收 1 |
| `CHECK 2 main 是当前（有「当前」徽章）` | S4 验收 2 |
| `CHECK 3 短 sha 是 8 位小写 hex` | S4 验收 3 |
| `CHECK 4 空仓库 data-state=ready` / `显示空态文案` | S4 验收 4 |
| `CHECK 5 切回后列表刷新（出现新分支 hotfix）` | S4 验收 5 |
| `CHECK D6 …`（初始 idle / role+tabindex / upstream / 选中态 / 键盘 / 断连清空 / 重连重取） | D6 的 DOM 契约 |
| `CHECK D6 删除仓库后点它 → data-state=error` / `按码映射文案` / `不是「绑定失败」` / `错误后可切到其它仓库恢复` | D6 的失败态（`REPO_NOT_BOUND` 的文案映射 + 错误不是终态） |

## S5 那份（`s5_diff.mjs`）

同一个骨架，验「点分支 → 看到 diff」（D11 的 10 条）：

1. 在临时工作区建**四个真仓库**：`demo-main`（main 在分支点后又提交了 `m.txt`，用来钉三点语义）、
   `demo-big`（feature 新增 5001 行 → patch > 缺省 `maxChars`，验截断）、`demo-xss`（feature
   新增文件名 `x<script>alert(1)<script>.txt`，验不可信文本）、`demo-detached`（游离 HEAD，
   验「不知道默认分支」时不发请求）；
2. 真内核 + 真 Chrome，填路径绑定四个仓库 → 点 `demo-main` 的仓库行 → 点 `feature` 分支行；
3. 断言 `#diff[data-state=ready]`、`#diff-stat` **逐字**等于 `2 个文件 · +3 −1`
   （`·` 是 U+00B7、`−` 是 U+2212，不是 ASCII 连字符）、两个输入框被填成 main/feature；
4. 着色：`.diff-add` / `.diff-del` / `.diff-hunk` 各 ≥1；`+++ b/` 那行的 class 是
   `diff-meta` **而不是** `diff-add`（D9 的分类顺序）；
5. 点**默认分支本身** → 回 `idle` + 「这是默认分支，选择其它分支查看差异」，且不发请求；
6. 手填 `main` / `feature` 点「对比」→ `ready` + stat 正确；
7. 键盘：聚焦分支行 + `Enter` → `ready`（与 click 同一条路径）；
8. 手填不存在的 ref → `error` + 文案「分支或提交不存在」（`ERROR_TEXT.BAD_REF`）；
9. XSS：`document.querySelector("#diff-patch script")` 为 `null`，且文件名作为**字面文本**
   出现在 `#diff-patch.textContent` 里；
10. 大 diff → `#diff-truncated` 可见、文案逐字匹配、`#diff-patch` 非空（渲染成行）；
11. 表单空值（trim 后任一为空）→ `#diff-error`「请填 base 和 head」且 `data-state` 保持原样；
12. 游离 HEAD 的仓库点分支 → `idle` + 「不知道默认分支（HEAD 未指向分支），请在上方填两个 ref」，
    且不发请求（stat/patch 不出现）；
13. **真的 SIGKILL 内核** → `#diff` 回 `idle` + 「已断开：差异未知」+ 清空 patch/stat。

截图与内核日志落在 `/tmp/postel-s5-acceptance-XXXX/`（结尾打印路径）。脚本还会顺带检查：
内核日志里没有未脱敏的 ticket、内核没有往三个用户仓库里写东西。

| 输出行 | 对应 |
|---|---|
| `CHECK 1 …`（ready / stat 逐字 / 输入框 / 标题 / 选中） | S5 验收 1 |
| `CHECK 2 …`（add/del/hunk ≥1、`+++ b/` 是 meta） | S5 验收 2（D9 分类顺序） |
| `CHECK 3 …`（默认分支 → idle + 文案 + 不发请求） | S5 验收 3 的默认分支分支 |
| `CHECK 4 手填两个 ref 对比` | S5 验收 3（手填） |
| `CHECK 5 …`（BAD_REF 可读文案、不是空白） | S5 验收 5 |
| `CHECK 6 …`（截断可见 + 文案逐字 + patch 非空 + stat 不受截断影响） | S5 验收 4 |
| `CHECK 7 …`（script 元素为 null + 字面文本在 textContent） | S5 验收 3 的 XSS 纪律（D10 第 18 行） |
| `CHECK 8 …`（聚焦 + Enter → ready） | S5 验收 7 |
| `CHECK 9 …`（中文文件名/内容可读） | S5 验收 3 的中文内容用例（D9） |
| `CHECK D9 空表单 …`（「请填 base 和 head」+ state 不变） | D9 交互表（表单空值） |
| `CHECK D9 游离 HEAD …`（idle +「不知道默认分支」+ 不发请求） | D9 交互表（defaultBranch=null） |
| `CHECK 10 …`（SIGKILL → idle + 「已断开：差异未知」+ 清空） | S5 验收 6 |
| 末尾 ticket / 仓库零改动检查 | 05 §4.1（S2 回归点）、S3 验收 5 的同一纪律 |

## S6 · 左栏 Bot 与它的 charter（`s6_bots.mjs`）

真 Chrome + 真内核 + 两个真仓库，覆盖 S6 的 D8 DOM 契约：

| 输出行 | 对应 |
|---|---|
| `S6 初始 #bots 是 idle …` / `初始 #charter …` / `初始 bot-list 为空` | 没选仓库不是「这个仓库没有 Bot」 |
| `1 恰好 1 行 Bot 且是 builder` / `权限徽标 L1` / `摘要「尚未填写职责」` | 验收 1/2（名单 + 权限 + 摘要） |
| `1 Bot 行键盘可达（role=button + tabindex=0）` | D8 键盘可达 |
| `3 记忆目录 = 「记忆目录：<状态根>/bots/<repoId>/builder」` | 验收 3 + D9（绝对路径，不伪装 `~`） |
| `3 charter 全文含「# builder」与「## 职责」` / `全文是一个 <pre>` | 验收 3（只读全文） |
| `4 切仓库 → #charter 回 idle、全文与记忆目录清空` | D8（切仓库清空，不留旧 charter） |
| `5 聚焦 Bot 行 + Enter → #charter ready` | D8（键盘与 click 同一条路径） |
| `6 charter 里的 <script> 不生成 script 元素` | 不可信文本：charter 只进 `textContent` |
| `7 断连前无 console 错误、无未捕获异常` | 页面无 JS 错误 |
| `8 断连 → …`（4 行） | D8（断连回 idle 并清空、`已断开：Bot 列表未知`） |

脚本还会检查：内核日志里没有未脱敏的 ticket（05 §4.1）、两个用户仓库零改动（记忆只在
状态根下）。截图与内核日志落在 `/tmp/postel-s6-acceptance-XXXX/`（结尾打印路径）。
