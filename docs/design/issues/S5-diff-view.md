# S5 · 点分支 → 看到 diff

- **里程碑** M1 · **预计** 2h · **依赖** S4（含 S4.5 的门禁与 DoD）· **优先级** P0
- **用户路径**：点某个分支 → 主区显示它与默认分支的差异（统计 + 着色 diff）；也能手填两个 ref 对比

## 背景

这是产品的核心动作之一——「看 diff」。M1 就要证明它可读、不卡、不丢内容。
也是**第一次把用户给的引用名送进 git**：所以本刀必须先把 U15「ref 名怎么过边界」裁决掉
（[09-slice-dod §1.4](../09-slice-dod.md) 的规则：依赖的未决项先裁决再开工）。

## 验收标准

1. 点击分支行，显示 `默认分支 → 该分支` 的 `N 个文件 · +X −Y` 统计与着色 diff。
2. diff 文本按行着色：新增绿、删除红、hunk 头单独色、文件头/元信息灰色。
3. 支持在面板里手动填两个 ref 进行对比（不限于分支）。
4. 内容超过上限时返回 `truncated: true`，UI 显示「已截断」提示，**不静默丢**。
5. 非法 ref 显示可读错误（`BAD_REF` 文案），**不是 500 / 不是空白**。
6. 切仓库 / 断连时差异面板回 `idle` 并清空，**不留旧 diff 装新鲜**（同 S4 D6 的纪律）。
7. 分支行**键盘可达**（`Tab` 能聚焦，`Enter`/`Space` 能触发），与左栏仓库行一致。

## 涉及文件

- `src/postel_studio/gitops/repo.py`（`resolve_commit` / `diff_stat` / `diff_patch` + 严格解码的 U15 修法）
- `src/postel_studio/api/repository.py`（`diff`：ref 校验 → 解析 → 取 stat/patch → 载荷）
- `src/postel_studio/api/gateway.py` / `src/postel_studio/api/app.py`（`repo.diff` 注册与注入）
- `src/postel_studio/web/assets.py`（`#diff` 面板）
- `tests/test_diff.py`（新建）、`tests/test_reverse_checks.py`（加 5 条注入）
- `tools/acceptance/s5_diff.mjs`（新建，真实浏览器）

## 禁止事项

- 不得在前端直接调用 git（数据只从 WS 帧拿）。
- 不得渲染未经转义的 HTML（diff 是**不可信文本**：文件名可以是 `<script>`）。
- 不得用固定宽度假设中文（中文字符宽度不同，需测）。

---

# 裁决与验收细则（管理层，实施前钉死）

> 本节的每条都是**契约**：实现可以换写法，但下面这些名字、字段、码、DOM id、常量值不许改。
> 上游：[07-api-contract.md](../07-api-contract.md) §2.2 / §4.2 / §1.3、[03-ui-spec.md](../03-ui-spec.md)、
> [06-data-model.md](../06-data-model.md) §8、[09-slice-dod.md](../09-slice-dod.md)、
> [06-pitfalls](../../codemap/06-pitfalls.md)（本刀结案 U15）。
> 规格里所有「实测」都是我（裁决者）在 `/tmp` 里**跑过**的命令，不是推断（S3.5 的学费）。

## D1 · 语义：三点 `base...head`（merge-base），不是两点

用户的意图是「这个分支**改了什么**」，所以取**共同祖先到 head** 的差异：

```text
        base ── c1 ── c2        ← main（默认分支）在分支点后又提交了 c2
          └──── f1              ← feature
repo.diff(base="main", head="feature") 必须只报 f1 的变化（insertions=1, deletions=0），
不得把 c2 报成删除（两点语义会）。
```

实测（`main` 在分支点后追加了 `d/m.txt`，`--numstat` 输出，`\344…` 就是 `中文.txt`）：

```text
$ git diff --numstat --no-renames main...feature     # 三点：看不到 main 自己后来的 d/m.txt
2	1	f.txt
1	0	"\344\270\255\346\226\207.txt"
$ git diff --numstat --no-renames main feature       # 两点：把 main 的追加报成删除
0	1	d/m.txt
2	1	f.txt
1	0	"\344\270\255\346\226\207.txt"
```

无共同祖先时（`git checkout --orphan` 造两个不相干历史）：

```text
$ git diff --numstat main...other
fatal: main...other：无合并基线        # exit 128，stdout 空
```

→ 翻 `BAD_REF`（码表里最贴的；**不新增码**），文案写「两个 ref 没有共同祖先，无法比较」。

## D2 · 先解析成 object id，用户的 ref 字符串**永不进 diff 命令行**

每个 ref 各跑一次 `git rev-parse --verify --end-of-options <ref>^{commit}`：

- `--verify` **只能收一个 rev**（实测多个参数回 `fatal: 需要一个单独的版本`），所以 base/head 两次调用；
- `--end-of-options` 让 `-` 开头的字符串不被当成选项（实测 `--no-index^{commit}` → exit 128，被拒）；
- `^{commit}` 强制 commit 类型：拿 tree id 来问会回
  `error: <sha>^{commit}: expected commit type, but the object dereferences to tree type` + exit 128 → `BAD_REF`；
- 解析结果**必须自己校验形状**（`^[0-9a-f]{40}$` 或 `^[0-9a-f]{64}$`），不许把 rev-parse 的回显直接当 id
  （无 `--verify` 时它会把有歧义的参数原样回显到 stdout，实测：`git rev-parse --end-of-options main^{commit} nosuch^{commit}`
  输出 `--end-of-options`、`<sha>`、`nosuch^{commit}` 三行且 exit 128）。

**之后的 stat / patch 命令只用两个 object id**（`<a>...<b>`）：用户字符串不出现第二次。
好处有两个：① 参数注入面收敛到一条命令（且它带 `--end-of-options`）；② base/head 在解析后不再会"移动"，
stat 与 patch 描述的是同一对 commit（不会出现"统计说 3 个文件、patch 里有 4 个"）。

## D3 · U15 裁决：ref 名怎么过边界（**本刀必须裁决的那一条**）

三条规则，按数据方向分。**身份类**数据（ref 名）与**显示类**数据（patch 正文）待遇不同，这是本裁决的核心：

| 方向 | 数据 | 策略 | 失败时 |
|---|---|---|---|
| 入站 | `base` / `head`（用户字符串） | 合法 UTF-8 + 形状检查 | `INVALID_PARAMS` |
| 出站 | ref 名（`repo.branches` / `repo.bind` / 解析） | **严格**解码，绝不 mangled 回传 | `NOT_SUPPORTED` |
| 出站 | patch 正文 / 文件名 | `replace`（只进 DOM，永不回传 git） | **不失败**（出现 `�` 可以） |

### D3.1 入站：只收合法 UTF-8 的名字

`base` / `head` 必须同时满足：`str`、非空、长度 ≤ 200、不含控制字符（`< 0x20` 或 `0x7f`）、
`value.encode("utf-8")` 不抛异常、不以 `-` 开头、`strip()` 后非空。任一不满足 → `INVALID_PARAMS`。

**为什么必须挡 lone surrogate（实跑，不是推测）**：JSON 允许 `"\udcff"` 这种转义，`json.loads` 会得到
一个 lone surrogate 的 `str`；Python 给子进程编码 argv 时用 `surrogateescape`，于是它在**字节层**变成
原始 `0xff`——正好是 git 里那个非 UTF-8 ref 名的字节：

```text
>>> json.loads('{"base":"\\udcff"}')['base']
'\udcff'                              # 合法 JSON 帧里就能带进来
>>> os.fsencode('bad-\udcff^{commit}')
b'bad-\xff^{commit}'                  # surrogateescape：没有报错，直接变成真字节
$ git -C <有 bad-\xff 分支的仓库> rev-parse --verify --end-of-options $'bad-\xff^{commit}'
5fbaba0669f41854fb02f7a522969c95c32011ed        # exit 0：**真的解析成功**
```

也就是说：不挡的话，`repo.branches` 拒绝展示的名字，客户端能用 JSON 转义"洗"回 git——
**一侧拒绝、另一侧接受 = 漏洞**。所以入站校验是这条裁决的实测依据，不是洁癖。

### D3.2 出站标识符：严格解码，宁可拒绝也不 mangled

`run_git()` 增加 `decode_errors: str = "strict"` 参数并原样传给 `subprocess.run(errors=...)`；
严格模式下解码失败抛**新异常** `GitOutputUndecodableError`（加进 `GIT_ERRORS`）。
边界层（`api/repository.py`）把它一律翻成 `NOT_SUPPORTED` +「仓库含非 UTF-8 的名称，第一版不支持」。

**不用 `errors="replace"` / `surrogateescape` 处理 ref 名**，理由（U15 原文已写过，本刀实测补强）：
mangled 的名字回传 git 会变成 `BAD_REF`，**甚至指向别的 ref**——比报错更糟。所以出站就是拒绝。

→ 这条同时把 **U15 结案**：`repo.branches` / `repo.bind` / `repo.diff` 三条路径从"未捕获 `UnicodeDecodeError`
→ `INTERNAL`（请查看终端）」变成"稳定的 `NOT_SUPPORTED` + 一句人话"。

实测触发（`git branch $'bad-\xff'` 之后）：

```text
$ git for-each-ref refs/heads/ --format='%(refname:lstrip=2)'
bad-\xff\nmain\n       # 原始字节；subprocess(text=True) 严格解码 → UnicodeDecodeError（position 4, byte 0xff）
```

### D3.3 出站内容：宽松解码

patch 正文与其中的文件名用 `decode_errors="replace"`。理由：内容只经 `textContent` 进 DOM，
**永不回传 git**；而仓库里存在非 UTF-8 的文件内容（latin-1 源码、历史遗留）是**常态**——
为它回 500 是错的。实测：

```text
$ git diff --no-color HEAD~1 HEAD | …      # 文件含 \xff\xfe
strict  → UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 212
replace → '-�� latin1 bad' / '+�� latin1 bad2'     # U+FFFD ×2，可正常显示
```

## D4 · 确定性：把影响输出的用户配置全部用 flag 钉死

研究这些配置**是因为它们会让同一个仓库对同一次 diff 给出不同答案**，不是过度设计。每条都实测过：

| 固定项 | 防住什么 | 实测 |
|---|---|---|
| `--no-color` | `color.ui=always` → patch 里混入 `\033[1m` 等 ANSI | 不传时 od 出来是 `033 [ 1 m`；传了干净 |
| `--no-ext-diff` | `diff.external=/bin/false` → patch 失败（`fatal: 外部 diff 退出，停止在 f.txt`，exit 128） | 传了正常出 patch |
| `--no-textconv` | `.gitattributes` + `diff.<driver>.textconv` 会把内容替换成外部命令的输出 | 语义（不跑用户命令） |
| `--no-renames` | `diff.renames` 缺省 **true**：重命名算 `0 0 old => new`（1 个文件）；用户设 false 就变 `1 0 new` / `0 1 old`（2 个文件）。**同一仓库两种答案** | 两种输出都实测到 |
| `--unified=3` | `diff.context` 改 hunk 大小 | — |
| `-c core.quotePath=false` | 缺省把非 ASCII 路径写成 `"\344\270\255\346\226\207.txt"`；关掉后是 `中文.txt` | 实测两种 |

**rename 的裁决**：MVP **关闭** rename 检测（`--no-renames`）。理由：开着的话结果还依赖 `diff.renameLimit`
（大 diff 会静默退化成不检测），为了"重命名显示得好看"再引入第三个配置依赖不值得。
重命名 = 一删一增（`filesChanged` 计 2）；以后要做 rename 感知视图，单独切片 + 单独裁决。

## D5 · `stat` 的精确定义

命令：`git -c core.quotePath=false diff --numstat -z --no-renames <a>...<b>`

解析：按 `\x00` 切记录，每条是 `add\tdel\tpath`；**只取前两段**（路径里可以含 tab 甚至换行——
`-z` 下路径原样输出，这正是用 `-z` 而不是按行切的原因；实测含换行的文件名被原样保留在 NUL 分隔的记录里）。
字段是 `-`（二进制）时按 0 计。

| `stat` 字段 | 定义 |
|---|---|
| `filesChanged` | numstat **记录数**（含二进制、含 gitlink、含纯 mode 变更） |
| `insertions` / `deletions` | 各记录的加/减行**求和**（`-` 记 0） |

实测的几种记录（od 输出，`\0` 是分隔符）：

```text
-\t-\tb.bin\0          # 二进制：计 1 个文件，0/0
1\t1\tf.txt\0          # 普通文本
1\t1\tvendor/s\0       # gitlink（子模块 commit 前进）：1/1
0\t0\tf\0              # 纯 mode 变更（chmod +x）：计 1 个文件，0/0
```

## D6 · 截断契约（`maxChars`）

| 项 | 值 / 规则 |
|---|---|
| 缺省 | `DEFAULT_MAX_CHARS = 100_000` |
| 合法区间 | `[MIN_MAX_CHARS = 1_000, MAX_MAX_CHARS = 200_000]` |
| 类型 | 必须是 `int` 且**不是 `bool`**（`True` 是 `int` 子类，`isinstance(True, int)` 为真——用 `type(v) is int` 或先排除 `bool`）。区间外 / 类型错 → `INVALID_PARAMS` |
| 不夹紧 | 越界**报错**，不静默夹到边界：夹紧会让调用方以为拿到了它要的长度 |
| `truncated` | `len(full_patch) > max_chars` |
| `patch` | `full_patch[:max_chars]`（**按字符**切，允许切在行中间：按字符上限就不做行对齐修正） |

两条**必须**测的边界：长度恰好 `max_chars` → `truncated=false`；`max_chars + 1` → `truncated=true` 且
`len(patch) == max_chars`。

**`stat` 不受截断影响**：统计描述完整差异，`truncated` 只影响 patch（否则"统计"跟着上限变形，等于撒谎）。

空差异（`base` 与 `head` 同一 commit）：`stat 0/0/0`、`patch: ""`、`truncated: false` —— **成功，不是错误**。

## D7 · 错误码映射（**禁止新增码**，07 §1.3 之内）

顺序是契约：**参数形状 → 资源存在 → ref → 语义**。

| # | 情形 | 码 |
|---|---|---|
| 1 | `repoId` 缺失 / 非字符串 / 空 | `INVALID_PARAMS` |
| 2 | `repoId` 查不到 | `REPO_NOT_BOUND` |
| 3 | `base`/`head` 缺失 / 非字符串 / 空 / 超长 / 控制字符 / lone surrogate / 以 `-` 开头 | `INVALID_PARAMS` |
| 4 | **仓库路径已不存在**（目录或 `.git` 被删） | `REPO_NOT_BOUND`（不许被 ref 错误盖住） |
| 5 | ref 解析不到（不存在 / 不是 commit） | `BAD_REF` |
| 6 | 无共同祖先（三点没有 merge base） | `BAD_REF` |
| 7 | git 不存在 | `ADAPTER_UNAVAILABLE` |
| 8 | git 超时 | `INTERNAL`（同 S3 D6，S19 的 doctor 再细化） |
| 9 | 输出含非 UTF-8 的 ref 名 | `NOT_SUPPORTED` |
| 10 | 成功（含空差异） | `ok: true` |

**#4 必须先判**：路径被删时 `rev-parse` 同样失败，不先探测就会把"仓库没了"报成"ref 不存在"
（用户会去改 ref，但真正的问题是仓库被删/搬走了）。要求有专门测试钉住（见 D10 第 15 行）。

## D8 · 分层落点（只向下 import；**不新增模块**、不改 02-layers 的层表）

| 层 | 位置 | 新增 |
|---|---|---|
| L0 | `gitops/repo.py` | `GitOutputUndecodableError`、`DIFF_FLAGS`、`DiffStat`、`resolve_commit`、`diff_stat`、`diff_patch`、`run_git(decode_errors=…)` |
| L3 | `api/repository.py` | 常量（码/文案/上限）+ `_require_ref` + `RepositoryService.diff` |
| L3 | `api/gateway.py` | `Providers` 加 `repo_diff`，`dispatch()` 加一行 |
| L3 | `api/app.py` | 注入 `repositories.diff` |
| L4 | `web/assets.py` | `#diff` 面板（HTML + CSS + JS 全在本文件，保持"无前端构建链"） |

L0 的三态沿用 S4 D4 的写法（**不许合并**）：

| 返回 | 含义 |
|---|---|
| `resolve_commit(root, ref) -> str` | 解析成功，返回 40/64 位小写 hex |
| `resolve_commit(...) -> None` | 问不出来（ref 不存在 / 不是 commit / 仓库问不出来） |
| `diff_stat(...) -> DiffStat` / `None` | git 答了 / 非零退出（无共同祖先等） |
| `diff_patch(...) -> str` / `None` | 同上 |
| 抛 `GitUnavailableError` / `GitTimeoutError` / `GitOutputUndecodableError` | 起不来 / 卡住 / 名字不是 UTF-8 |

**格式化在边界**：截断（D6）与 `camelCase` 字段名都发生在 `api/repository.py`，L0 只给完整、
snake_case 的事实（同 S4「sha 截 8 位在边界」）。这样两层各自可测。

## D9 · DOM 契约（浏览器验收按这些 id / class 点）

主区新增一节，**位置在 `#branches` 之后**（不重排既有布局）：

```text
<section id="diff" data-state="idle" aria-live="polite">
  <h2 id="diff-title">差异</h2>
  <form id="diff-form" autocomplete="off">
    <input id="diff-base" type="text" spellcheck="false" placeholder="base（分支或提交）">
    <span class="diff-arrow">→</span>
    <input id="diff-head" type="text" spellcheck="false" placeholder="head（分支或提交）">
    <button id="diff-compare" type="submit">对比</button>
  </form>
  <p id="diff-state">点左侧分支查看它与默认分支的差异，或在上方填两个 ref</p>
  <p id="diff-error" role="alert" hidden></p>
  <p id="diff-truncated" hidden>已截断：差异太大，只显示前一部分</p>
  <p id="diff-stat" hidden></p>
  <pre id="diff-patch" hidden></pre>
</section>
```

| 元素 / 属性 | 规则 |
|---|---|
| `#diff[data-state]` | `idle`（未选/未选分支/已断开）/ `loading` / `ready` / `error`；显隐**只由 CSS** 决定，JS 只改 `data-state`（T8 的教训） |
| `#diff-title` | 未选仓库时 `差异`；选中仓库后 `差异 · <repoName>`（`textContent`，不拼 HTML） |
| `#diff-stat` | `ready` 时显示，文本固定为 `<N> 个文件 · +<X> −<Y>`（`·` 是 U+00B7，`−` 是 U+2212，**不是** ASCII `-`） |
| `#diff-patch` | `ready` 时显示；`patch` 为空串时 `hidden`（空差异只显示 stat） |
| `#diff-truncated` | `truncated === true` 时可见，文案就是 HTML 里那句（不动态拼数字） |
| `#diff-error` | `data-state=error` 时可见，按**错误码**映射中文（复用 `ERROR_TEXT` + 本面板自己的兜底） |
| 行元素 | 每行一个 `<div class="diff-line diff-<kind>">`，`<kind> ∈ {add, del, hunk, meta, ctx}`；文本一律 `textContent` |
| 行分类顺序 | **必须按这个顺序判**（`+++ `/`--- ` 是文件头，不能算 add/del；`+`/`-` 前缀不能先判） |

行分类顺序（实现照抄，验收会专门钉第 2、3 条）：

| 顺序 | 前缀 | kind |
|---|---|---|
| 1 | `diff --git ` / `index ` / `new file mode ` / `deleted file mode ` / `old mode ` / `new mode ` / `\` | `meta` |
| 2 | `@@` | `hunk` |
| 3 | `+++ ` / `--- ` | `meta` |
| 4 | `+` | `add` |
| 5 | `-` | `del` |
| 6 | 其它 | `ctx` |

交互与状态：

| 情形 | 行为 |
|---|---|
| 点分支行 | 该行 `data-selected="true"`（其它行清掉）；`base` = 仓库 `defaultBranch`，`head` = 该分支；两个输入框同时被填上（用户可改了再点「对比」） |
| 点的是默认分支本身 | **不发请求**：`idle` +「这是默认分支，选择其它分支查看差异」 |
| 仓库 `defaultBranch` 为 `null`（游离 HEAD） | **不发请求**：`idle` +「不知道默认分支（HEAD 未指向分支），请在上方填两个 ref」 |
| 手动提交表单 | trim 两个输入；任一为空 → `#diff-error` 显示「请填 base 和 head」（`data-state` 保持原样）；否则发请求 |
| 切仓库 | `idle` + 清空 patch/stat/error/选中行；标题跟着仓库名变 |
| 断连 | `idle` +「已断开：差异未知」+ 清空（同 S4 的分支面板纪律） |
| 重连且仍有选中仓库 | 不自动重发 diff（用户没点过分支就没有 base/head 可发；有 `base`/`head` 输入框时也不自动重放）。**这一条与 S4 分支列表不同，是故意的不对称**：diff 是"查询历史动作"，重放它会覆盖用户正在看的对比 |
| 快速切换 | `diffRequestToken` 拦住过期回包（同 S4 的 `branchRequestToken`） |
| 键盘 | 分支行 `tabindex="0"` + `role="button"`，`Enter`/`Space` 与 click 同一条路径 |
| 安全 | 全文件 `innerHTML` 保持零使用（`textContent` only）；有静态测试钉住 |

CSS 颜色（暗色底 `#0f1115` 上可读）：`.diff-add{color:#3fb950}` `.diff-del{color:#f85149}`
`.diff-hunk{color:#58a6ff}` `.diff-meta{color:#8b949e}` `.diff-ctx{color:inherit}`；
`#diff-patch{max-height:60vh; overflow:auto; white-space:pre-wrap}`，字体 `ui-monospace, monospace`，
**不设固定宽度、不假设中文宽度**（验收里有一条中文内容用例）。

## D10 · 验收 → 测试映射（先写表，再写代码）

| # | 验收 / 契约 | 测试（`tests/test_diff.py` 内，另注明者除外） |
|---|---|---|
| 1 | stat 精确：真实仓库已知 fixture，三个数都断言（**禁止 `a or b` 式断言**） | `test_stat_exact_numbers` |
| 2 | 三点语义：main 在分支点后再提交，diff 不得把它算进去（D1） | `test_three_dot_ignores_later_base_commits` |
| 3 | 二进制文件：计 1 个文件、0/0（D5） | `test_stat_counts_binary_file_without_line_counts` |
| 4 | 纯 mode 变更（chmod）：计 1 个文件、0/0 | `test_stat_counts_mode_only_change` |
| 5 | 重命名按一删一增计 2 个文件（`--no-renames` 的可见后果，D4） | `test_rename_counts_as_delete_plus_add` |
| 6 | 空差异：`0/0/0` + `patch==""` + `truncated=false` | `test_same_ref_is_empty_diff_not_error` |
| 7 | patch 逐行种类（add/del/hunk/meta/ctx 各至少一行，由服务端返回的文本断言） | `test_patch_contains_expected_line_kinds` |
| 8 | 截断边界两侧 + `len(patch)==maxChars`（D6） | `test_truncation_boundary_both_sides` |
| 9 | `maxChars` 非法：`0` / `999` / `200001` / `"1000"` / `true` / `1.5` → `INVALID_PARAMS` | `test_max_chars_rejects_out_of_range_and_bool` |
| 10 | 非法 ref：不存在的名字、tree id、`-` 开头、含 `\n`、lone surrogate → 各回对应码（D3.1/D7） | `test_bad_refs_are_bad_ref`、`test_ref_with_lone_surrogate_is_invalid_params` |
| 11 | 注入面：`base="--no-index"` 之类不得被当成 git 选项 | `test_ref_starting_with_dash_is_rejected` |
| 12 | 用户配置隔离：仓库设 `color.ui=always` → patch 内**没有** `\x1b`（D4） | `test_patch_has_no_ansi_when_color_ui_always` |
| 13 | 用户配置隔离：`diff.external` 指向假命令 → 仍然成功（`--no-ext-diff`） | `test_patch_ignores_external_diff_driver` |
| 14 | 非 UTF-8 文件内容 → 成功、含 U+FFFD、不 500（D3.3） | `test_non_utf8_content_is_replaced_not_fatal` |
| 15 | 仓库路径被删 + 任意 ref → `REPO_NOT_BOUND`（**不是** `BAD_REF`，D7 #4） | `test_deleted_repo_path_is_repo_not_bound` |
| 16 | 无共同祖先 → `BAD_REF`（D1） | `test_unrelated_histories_are_bad_ref` |
| 17 | 中文文件名在 patch 里是**字面**中文（`quotePath=false`），不是 `\346` 转义 | `test_non_ascii_path_is_readable_not_escaped` |
| 18 | XSS：patch 里出现原始 `<script>` 文件名时，页面渲染路径只用 `textContent` | `test_page_never_uses_inner_html`（静态：`assert "innerHTML" not in INDEX_HTML`）+ 浏览器验收第 7 条 |
| 19 | U15 结案：仓库有非 UTF-8 分支名 → `repo.branches` 回 `NOT_SUPPORTED`（不再 `INTERNAL`） | `test_branches_non_utf8_refname_is_not_supported` |
| 20 | U15 结案：HEAD 指向非 UTF-8 分支时 `repo.bind` 回 `NOT_SUPPORTED`；HEAD 正常时该仓库仍能绑 | `test_bind_non_utf8_branch_is_not_supported` |
| 21 | 端到端（真 WS 帧）：`repo.diff` 回 `{data:{stat,patch,truncated}, seq}`，与直调服务层一致 | `test_ws_repo_diff_matches_service` |
| 22 | 既有测试要改一处：`tests/test_gateway.py` 的未实现方法反例列表里若有 `repo.diff`，必须移出（仍需 ≥4 个反例） | `test_unknown_method_is_unknown_method_not_subscribe` |
| 23 | 反向注入 5 条（D12） | `tests/test_reverse_checks.py` |
| 24 | 浏览器验收：见 D11 的清单 | `tools/acceptance/s5_diff.mjs` |

`stat` 的断言要求（验收 1 的补充）：**三个数字各自独立断言**，且 fixture 的期望值写死在测试里
（例：`assert payload["stat"] == {"filesChanged": 2, "insertions": 3, "deletions": 1}`）。

## D11 · 浏览器验收清单（`tools/acceptance/s5_diff.mjs`，真实 Chrome）

沿用 S4 脚本的骨架（起内核 → 拿 ticket → puppeteer 连 → `CHECK ✓/✗` 计数 → 缺 Chrome 时 `SKIP` 并 exit 0）。
**至少**覆盖：

1. 点 `feature` 分支行 → `#diff[data-state=ready]`，`#diff-stat` 文本**逐字等于** fixture 的期望串（含 U+00B7/U+2212）。
2. 着色行：`.diff-line.diff-add` 与 `.diff-line.diff-del` 各 ≥1，`.diff-line.diff-hunk` ≥1；
   `+++ b/` 那一行是 `diff-meta` **而不是** `diff-add`（钉 D9 的分类顺序）。
3. 点默认分支行 → `data-state=idle` + 文案「这是默认分支，选择其它分支查看差异」。
4. 手动填 `main` / `feature` 点「对比」→ `ready` + stat 正确。
5. 手填不存在的 ref → `data-state=error` + `#diff-error` 文案等于 `ERROR_TEXT.BAD_REF`。
6. 大 diff（脚本用 5000 行文件制造）→ `#diff-truncated` 可见、文案逐字匹配、`#diff-patch` 非空。
7. XSS：文件名 `x<script>alert(1)<script>.txt`（**不能含 `/`**，否则不是合法文件名）→
   `document.querySelector("#diff-patch script") === null` 且 `#diff-patch` 的 `textContent` 含该字面量。
8. 键盘：聚焦分支行 → `Enter` → `ready`。
9. 中文内容/文件名可读（用例 6 的文件里混入中文行即可）。
10. 断连（杀掉内核）→ `#diff` 回 `idle` + 「已断开：差异未知」。

**并且必须重跑** `tools/acceptance/s3_bind.mjs` 与 `tools/acceptance/s4_branches.mjs`（DoD §3.2：
前端被多刀共用）。**不许改坏 S4 的分支行结构**（`li[data-branch-name]` 与 `.branch-name` / `.branch-sha` /
`.branch-current` / `.branch-upstream` 的既有断言必须继续过）。

## D12 · 反向注入（`tests/test_reverse_checks.py`，每条只注入一类缺陷）

| # | 注入 | 应当变红的测试 |
|---|---|---|
| 1 | `DIFF_FLAGS` 里去掉 `--no-color` | `test_patch_has_no_ansi_when_color_ui_always` |
| 2 | `DIFF_FLAGS` 里去掉 `--no-ext-diff` | `test_patch_ignores_external_diff_driver` |
| 3 | `run_git` 的缺省 `decode_errors` 从 `"strict"` 改成 `"replace"` | `test_branches_non_utf8_refname_is_not_supported` |
| 4 | 解析 ref 时去掉 `^{commit}` 后缀 | `test_bad_refs_are_bad_ref`（tree id 不再被拒） |
| 5 | `#diff-patch` 的一行渲染 `textContent` 改成 `innerHTML` | `test_page_never_uses_inner_html` |

## D13 · 文档与门禁

- `07-api-contract.md`：**§4.6 `repo.diff`** —— 请求/响应示例（camelCase）、`stat`/`patch`/`truncated` 三字段表、
  `maxChars` 规则表、D7 的错误码表、三点语义一句话；§1.3 的 `NOT_SUPPORTED` 描述补"非 UTF-8 ref 名"这一例。
- `03-ui-spec.md`：差异面板一节（状态机 + 着色规则 + 截断提示），与 D9 对齐。
- `06-data-model.md` §8「明确不存的东西」加一行：**diff 不落库、不缓存**，每次按需从 git 读（视图实体）。
- codemap：`03-modules/{gitops,repository,gateway,app,assets}.md`、`03-modules/README.md` 行数表、
  `04-capabilities.md`（加「看 diff」）、`05-change-impact.md`（文件→门禁/盲区表）、
  `06-pitfalls.md`：新增 **T34**（用户配置会改 diff 输出：color/extdiff/renames/quotePath，各有 flag）、
  **T35**（三点 diff 无共同祖先不是 500 而是 `BAD_REF`）、**T36**（`--numstat` 必须 `-z`：路径可含制表/换行）；
  **U15 → ✅ 结案（S5）** + 新增 **U16**（patch 全量读进内存，无字节上限 → 到期 S18）。
- `bash scripts/check.sh all` 全绿，`bash scripts/check.sh build` 绿（DoD §3.1）。

## D14 · 预算与禁止事项

**`src/` 预算**：预计 **5 个文件 / 约 380–420 行**（口径见 [04 §1.1](../04-task-breakdown.md)：只计 `src/`）。
估计已经贴着 400 上限，**做完如实报**；若超，像 S4 那样在 PR 体里说明超了哪部分、为什么不在本刀拆
（不许为了凑数砍验收项）。

禁止：不加 HTTP 路由、不加错误码、不加依赖（含前端构建链）、不写 git（只读）、不缓存 diff、
不支持 pathspec（只到 `<a>...<b>`）、不开 rename 检测、不做语法高亮（只按行着色）、
浏览器验收脚本不进 `pyproject.toml`、不进 CI（与 S3/S4 同一条裁决）。

---

# 实施后的实测偏差与结论（管理层补记，2026-11-27）

> 规格是契约，但**契约不该只记录"当初怎么想"**——实施中实测出来的偏差、预算的真实数字、
> 以及"为什么接受了偏差"，都补在这里，给下一个读这份规格的人看（S4.5 的同一纪律）。

## E1 · 接受的偏差

| 偏差 | 实测原因 | 裁决 |
|---|---|---|
| D8 新增 `STAT_FLAGS`（= `DIFF_FLAGS` 去掉 `--unified`） | `--numstat` 隐含 `--no-patch`，但 `--unified` 隐含 `--patch` 并把它覆盖回来：`git diff --numstat -z --unified=3 A...B` 的输出是「numstat 记录 + 空行 + 完整 patch」，按 NUL 切会把 patch 当成第 2 条记录 | **接受**。D5 的命令本来就没有 `--unified`；已记 T36 |
| D9 `#diff-error` 的显隐用 `hidden` 属性（不是 `data-state`） | D9 要求「表单空值 → `#diff-error` 显示、`data-state` 保持原样」：若错误元素由 `data-state=error` 隐藏，idle 下就显示不出来 | **接受**（与 S4 `#branches-error` 同款；其余元素仍由 `data-state` 控制） |
| D13 07 §4.2 保留原简例，新增 §4.6 | 既有 §4.3–§4.5 编号不漂移 | **接受** |

## E2 · 预算实数：**超限，如实报，不拆**

`src/` 实测 **5 文件 / +621 −20 = 641 行**（上限 400，D14 预估 380–420）：

| 文件 | 增删 | 为什么这么重 |
|---|---|---|
| `web/assets.py` | +269/−4 | D9 的 HTML + CSS + JS 全在这一个文件（无前端构建链，禁止事项） |
| `gitops/repo.py` | +192/−11 | L0 原语（`resolve_commit`/`diff_stat`/`diff_patch`/`DIFF_FLAGS`/新异常）+ 本仓库的 docstring 密度 |
| `api/repository.py` | +151/−2 | ref 形状校验、截断、10 行错误码映射表 |
| `api/gateway.py` / `api/app.py` | +9/−3 | 接线 |

**决定：不拆成两刀。** 理由：① 三块合起来才是**一个可见能力**，拆开后 S5a 是"后端能跑但页面
空白"的半刀，违背「每刀都能演示」；② 拆开不减少总工作量，只增加一次 PR/审查/门禁的成本；
③ 没有任何验收项被删去凑数。**如实报超**，并在 PR 体里单独标明。

**给我的教训（写下来免下次再错）**：D14 估 380–420 行偏低了 55%。这个仓库的注释密度是刻意的
（S4 审核退回的学费换来的），估 `src/` 行数不能只数"逻辑行"，要按**同密度注释 + docstring**算；
下次遇到"新 L0 原语 + 新面板 + 接线"三类齐活的一刀，直接按 600+ 估，超预算就在**规格阶段**拆。

## E3 · 交付证据（管理层自己跑的，不是转抄实现者的）

| 证据 | 结果 |
|---|---|
| `bash scripts/check.sh all` | `272 passed` / `Total coverage: 84.13%` / lint·format·types·docs·layers·codemap·secrets 全 ✅ |
| `bash scripts/check.sh build` | `0.1.2` ✅ |
| 三个浏览器验收（真 Chrome，无 SKIP） | S3 `13/13`、S4 `28/28`、S5 `35/35` |
| 反向注入（`tests/test_reverse_checks.py`） | `32 passed`（含 S5 的 5 条） |
| 独立审计 · 静态契约 | 60 项，仅「`src/` 预算」1 项不满足（见 E2，非缺陷） |
| 独立审计 · 接口边界（真内核 + 真 WS + 真 git） | 38 项全过 |
| 独立审计 · DOM 契约（自写 puppeteer 脚本，不读实现者的验收脚本） | 26 项全过 |
| 复审抽查的"实测"声明 | T34 的 textconv、T36 的 `--numstat + --unified` 都**自己复跑验证成立** |

## E4 · 未验（承认，不假装）

1. 「重连且仍有选中仓库 → 不自动重发 diff」：验收脚本用 SIGKILL 杀内核，无法重连验证该路径。
2. 「快速切换时 `diffRequestToken` 丢过期回包」：实现存在，无稳定复现的专项测试。
3. 大 diff 的渲染性能/滚动观感（只验了截断文案与 3574 行的 DOM 结构，未测帧率、未做像素级中文换行检查）。
4. `maxChars` 不在 UI 暴露，只有服务层/WS 测试覆盖。
