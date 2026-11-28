# S16 · 只读 Bot 尝试 push → 被真正拦住

- **里程碑** M3 · **预计** 4h · **依赖** S10 · **优先级** **P2 最高**
- **用户路径**：把 Bot 调到 L0 → 派一个「试着 git push」的任务 → 命令被拒、磁盘无副作用、日志显示拦截

## 背景

**如果越权拦不住，产品不该上线。** Agent 没有默认沙箱，权限必须落在真实执行路径上，不能靠 Prompt 约束。

## 验收标准

1. L0（只读）Bot 的写操作被**实际阻断**，命令未执行。
2. L1 Bot 允许改 worktree，但 push 被阻断。
3. L2 Bot 允许 push 到本仓库、**不允许** push 到其他仓库。
4. 阻断发生在**执行前**：用写哨兵文件的命令验证，被拦时文件不存在。
5. 每次阻断有审计记录（时间、Bot、命令、判定结果）。
6. 若某路径无法阻断，**必须**在 UI 与文档标为不可用，并禁止该 Bot 获得对应权限。

## 涉及文件

- `src/postel_studio/domain/permission.py`（新建，纯函数判定）
- `src/postel_studio/agent/gate.py`（新建，执行前裁决层）
- `src/postel_studio/orchestrator.py`（接入裁决）
- `src/postel_studio/web/assets.py`（审计记录展示）
- `tests/test_permission_gate.py`（新建，必须真实执行）

## 测试要求

- **真实执行**：命令写哨兵文件，断言被拦时该文件**不存在**。
- 三个权限级别各一用例。
- 审计记录断言。

## 禁止事项

- 不得用 Prompt 约束替代真实阻断。
- 不得只看返回值不看副作用。
- 不得把无法阻断的路径静默放过。
- 不得在判定里读取 Agent 类型。

## 1. Decisions

- **D1 — 阻断落在 opencode v2 的 `permissions` 规则上，不落在 prompt 上。** opencode v2.4.2 的
  顶层 `permissions: [{action, resource, effect}]` 在**工具调用执行前**裁决；`effect: "deny"`
  时工具返回 `permission.rejected`，命令根本不进 shell。规则按**后匹配优先**叠加，我们在
  `OPENCODE_CONFIG_CONTENT` 里把规则随会话注入（与 S13/S14 凭据快照同一机制，key 仍只留在
  opencode 自己的 db 里，判定代码从不读 Agent 类型或 key）。`Permission` 阶梯的事实源仍是
  `domain/models.py` 的 `permission_covers`；`domain/permission.py` 只把级别**翻译**成规则，
  不另造一套大小比较。
- **D2 — 四个级别的规则形状（契约）**：
  - `L0`：`edit * = deny` + `shell * = deny`（只读；读/glob/grep 不限）。
  - `L1`：`edit * = allow`，然后 deny **所有** push 形状（可改 worktree，不可 push）。
  - `L2`：同上，后接 `git push origin *` / `git -C * push origin *` = allow
    （只许推本仓库的 `origin`；最后匹配优先）。
  - `L2+`：`edit * = allow`，不追加 push 拒绝（S16 只验收 L0/L1/L2；跨仓库能力在 S17 前保持
    "认识但谨慎"，`L3/L4` 仍不可赋值）。
  - **push  deny 的五种形状缺一不可**（review must-fix 1）：`git push *` / `git push` /
    `git -C * push*` / `git * push *` / `*/git push *`。opencode 的 shell resource 是
    scanner 产出的命令字符串；**只写 `git push *` 时 `git -C <repo> push` 不匹配**，实测
    哨兵出现且 bare remote 收到 ref（fail-open）。scanner 遇到无法安全解析的命令（如
    `eval`）时，只要存在任一 shell deny 规则就直接拒绝，因此未识别的 push 语法也 fail-closed。
  任何无法解析的权限值 → **fail-closed**：按 L0 规则处理，并在审计里记 `FALLBACK`。
- **D3 — 哨兵文件不能被 `edit/write` 工具绕过去。** 验收用 `git push origin main && touch
  sentinel.txt`：被拦时文件必须不存在。实测（§4）发现只 deny `shell "git push *"` 时，LLM 有时
  改用 `write` 工具直接创建哨兵，造成假阳性。因此测试配置**额外 deny `edit sentinel.txt`**
  （仅针对测试哨兵路径，不改产品规则），保证哨兵只能由那条 shell 命令产生。prompt 同时明确
  "只用 shell 工具执行这一条命令"。这不是放宽产品判定，而是把探针钉在唯一路径上。
- **D4 — 审计写在状态根的 `audit.jsonl`，一行一条 JSON。** 字段：`at`（epoch 秒）、`botId`、
  `permission`、`action`、`resource`、`effect`（`ALLOW`/`DENY`/`FALLBACK`）、`source`
  （`gate`/`agent`）。规则生成时由 gate 写 `source=gate` 的规则判定；Agent 实际发起的工具调用
  经 ACP `session/update` 的 `tool_call_update` 捕获命令与 `status`，任务结束时写 `source=agent`
  的实测行。两次记录都只含 bot/命令/判定，**不含 key、不含 prompt 正文**。
- **D5 — 无 Agent 时明确 skip，不假装拦住。** 没有 opencode 或快照里没有 active deepseek 凭据
  时，真执行用例 `pytest.skip("S16: opencode/deepseek unavailable")`；纯函数与审计用例仍跑。
  skip 必须写明原因，不输出 green。
- **D6 — 无法阻断的路径 fail-closed 并在 UI/文档标为不可用。** 未知权限值、未登记的 action、
  opencode 版本不支持 `permissions` 时，会话**不启动**，任务落 `failed` + 既有 `INTERNAL`
  （不新增错误码，07 §1.3 不变），审计记 `FALLBACK`；页面在 Bot 权限徽标旁显示"该级别当前
  不可用"，文档 06-pitfalls 记一条 U。绝不静默放行。
  **实现（review must-fix 2）**：`Session.__init__` 在 `initialize` 之后、`session/new` 之前
  用 `supports_top_level_permissions(agentInfo.version)` 探测（`major>=2` 才认，缺失 / 非数字 /
  `1.x` 一律不认）。不识别时抛 `AcpPermissionsUnsupportedError`（`AcpProtocolError` 子类，
  orchestrator 仍翻 `INTERNAL`）；`gate` 接住后写 `FALLBACK` 审计（`resource=agent-permissions-unsupported`）。
  页面 `loadAudit` 见 `FALLBACK` 行就把该 Bot 徽标标「（不可用）」。假 ACP 的 `v1` 模式把每个
  方法写进 logfile，断言 `session/new` 从未发出——是"会话没启动"，不是只把任务标红。
- **D7 — U20 裁决：顺延 S20（M3 验收）并写死到期日。** S16 是"拦不住就不该上线"的 P2 安全项，
  本刀的 `src/` 预算应集中在权限规则、执行前阻断与审计；`dispatch()` 的同步
  `agent.capabilities`（U20，最坏 30s 冻结 WS）是性能问题，与权限门同碰网关但改动形状不同
  （要把网关改 async + `asyncio.to_thread`）。记入 06-pitfalls：**S20 必须收口，不再顺延**。

### `src/` 预算估计（04 §1.1）

预计 7 文件 / ≤ 220 行：`domain/permission.py`（新）、`agent/gate.py`（新）、`acp_client.py`
（捕获 tool_call）、`orchestrator.py`（注入+审计）、`web/assets.py`（审计徽标）、
`api/app.py`（传 gate）、`storage/paths.py`（audit 路径）。

## 2. Acceptance → test map

| # | Acceptance criterion | Enforced by |
|---|---|---|
| 1 | L0 writes actually blocked | `tests/test_permission_gate.py::test_l0_write_blocked_before_execution`（真 opencode + 哨兵不存在 + 审计行）|
| 2 | L1 may edit, push blocked | `::test_l1_edit_allowed_push_blocked`（真执行：改文件成功、push 哨兵不存在）+ `::test_l1_git_c_push_blocked_before_execution`（review must-fix 1：真执行 `git -C <repo> push`，哨兵与 bare remote 的 ref 都不存在） |
| 3 | L2 push to origin only | `::test_l2_origin_push_allowed_other_push_blocked` + `::test_l2_git_c_foreign_push_blocked_before_execution`（`git -C <repo> push <foreign>` 真执行拦住） |
| 4 | Blocking is pre-execution | 上述各例的 `sentinel.txt` 断言 + `::test_control_without_rules_creates_sentinel`（无规则时哨兵**必须**存在，证明探针有效） |
| 5 | Audit per denial | `::test_audit_records_denial_with_bot_command_and_result`（读 `audit.jsonl`，断言时间/bot/命令/判定） |
| 6 | Unblockable path is visible | `::test_unknown_permission_fails_closed` + `::test_v1_agent_never_starts_session_and_audits_fallback` + `::test_page_marks_unavailable_level` + 注册反向注入 `INJECTIONS['S16 #2/#5/#6']`（抹 FALLBACK / 抹版本探测 / 抹徽标 → 对应测试红） |
| shapes | Every push shape denied | `::test_all_push_shapes_are_denied`（纯函数覆盖 `git push` / `git -C` / 绝对路径 / `-c` / 复合 `&&`）+ 反向注入 `S16 #1/#3/#4` |
| DOM | 审计展示 / 不可用标记 | `::test_permission_badge_contract` |

## 3. Boundary matrix

| 实体 / 协议 | 状态 | 期望 |
|---|---|---|
| Bot permission | `L0` / `L1` / `L2` / `L2+` / `L3` / `L4` / `None` / `"l1"` | 前四级按 D2；后四级 fail-closed（`FALLBACK`），不读 Agent 类型 |
| shell 命令 | `git push origin main` / `git push <path>` / `git -C <repo> push` / 绝对路径 `/usr/bin/git push` / `git -c … push` / 复合 `&&` / `pwd` | 五种 deny 规则全覆盖（D2）；复合命令任一子命令 deny 则整条拒绝；无法解析的命令在有 deny 规则时 scanner 直接失败 |
| 工具 | `shell` / `edit` / `write` / `patch` / `read` / `glob` / `grep` | `edit` 规则覆盖 `write`/`patch`；L0 只放行读类 |
| 哨兵 | 由 `shell` 创建 / 由 `edit` 创建 / 不存在 | 测试 deny `edit sentinel.txt`，只允许 shell 路径 |
| Agent | opencode v2.4.2 可用 + 有 deepseek / 无 opencode / 无凭据 / **v1 版本** | 真跑 / 明确 skip / 明确 skip / **不开会话 + `FALLBACK` + `failed`/`INTERNAL`** |
| 审计 | 规则判定 / 实际工具调用 / 无写权限 | 都落 `audit.jsonl`；写失败时任务 fail-closed |

## 4. 可执行证据（spec 阶段作者已跑）

环境：opencode v2.4.2、deepseek-flash、临时 `HOME` + `XDG_DATA_HOME` 只读快照（S14 机制）。
命令：`/tmp/probe_s16.py`（三级别 + 对照）、`/tmp/probe_s16_trace.py`（ACP 工具追踪）、
`/tmp/probe_s16_stable.py`（L1 三次稳定）。原始输出：

```text
l0-write: stop=end_turn 5.7s sentinel_exists=False
l1-push:  stop=end_turn 7.8s sentinel_exists=True   <-- 未 deny edit 时 LLM 用 write 工具造哨兵（假阳性）
l2-push-origin: stop=end_turn 5.0s sentinel_exists=True
=== controls ===
control-write: stop=end_turn 2.4s sentinel_exists=True
control-push:  stop=end_turn 5.0s sentinel_exists=True
l2-other-push: stop=end_turn sentinel_exists=False
```

ACP 追踪（L1）：`tool_call_update ... title="git push origin main && touch sentinel.txt" ...
status="failed"`，Agent 文本含 `{"error":{"type":"permission.rejected","message":"Permission
denied: shell"}}`，并明确"没有绕过限制去拆分命令"——**命令未进 shell**。

钉死哨兵路径后（`edit sentinel.txt = deny` + prompt 只用 shell），L1 三次：

```text
run0: stop=end_turn 3.9s sentinel=False
run1: stop=end_turn 3.8s sentinel=False
run2: stop=end_turn 6.0s sentinel=False
```

Review must-fix 1 的复现与修复证据（同一真 opencode + deepseek-flash，`git -C <repo> push
origin main && touch sentinel.txt`；`/tmp/probe_s16_rules.py` 原始输出）：

```text
old:          sentinel=True  refs='refs/heads/main'   <-- 只写 `git push *`：fail-open
plus-git-C:   sentinel=False refs='refs/heads/main'   <-- 加 `git -C * push *`：工具调用 failed
deny-all-git: sentinel=False refs='refs/heads/main'
```

（探针按同一 bare remote 顺序运行，ref 是上一轮留下的；判定以本轮哨兵与工具 `status=failed`
为准。新增的真执行用例 `test_l1_git_c_push_blocked_before_execution` 用一个全新的 bare remote
断言 `refs == ''`，不依赖顺序。）

## 5. Open questions / U 项

- **U20**（`dispatch()` 同步阻塞事件循环）：D7 裁决顺延 S20，S20 必须收口。
- **U1**（server.json 读竞态 flake）：S15 已顺延 S20，本刀不改；真执行用例不依赖该路径。
- **U22**（opencode v1 不识别顶层 `permissions`）：已按 D6 实现 fail-closed（握手后探测版本 → v1 不开会话 / `FALLBACK` / `INTERNAL` / 徽标不可用），在 06-pitfalls 登记并标记收口。
