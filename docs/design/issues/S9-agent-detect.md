# S9 · Click "Detect Agent" → see what the local agent actually declares

- **Milestone** M2 · **Estimate** 3h · **Depends on** S2 · **Priority** P0
- **User path**: click "Detect Agent" → the page shows whether an agent is available on this
  machine, which version it is, and the capabilities it declared during the handshake.

## 1. Why this slice, and what it refuses to do

Before dispatching work (S10) the kernel has to answer one question honestly: *is there an agent
here, and what does it claim to support?* S9 is **handshake + display**. It starts the agent
process, performs exactly one ACP `initialize`, renders the answer, and reaps the process.

It deliberately does **not**: create a session, send a prompt, write a task, touch `state.db`,
or grant any permission. Those belong to S10 and later.

The whole point of the slice is that the answer comes from the agent, not from us. `02b §4`
states the rule and `04 §6` forbids the alternative: capabilities are read from the handshake
response, never hard-coded, and no method name or branch condition may contain a product name.

## 2. Decisions (D1–D13). These are contract; implementation may not re-decide them.

### D1 · One channel id: `acp-stdio`. It names the transport, not the product

`agentKind` is `"acp-stdio"`. It describes *how* the kernel talks to the agent (ACP over stdio,
`02b §1`), not *who* the agent is. `"opencode"` never appears as an id, a method name or a branch
condition — it is only the default executable in the registry (D2).

The registry is a table, not a constant:

```python
@dataclass(frozen=True)
class Channel:
    agent_kind: str  # "acp-stdio"
    command: tuple[str, ...]  # ("opencode", "acp")
```

`channels()` returns every entry; `resolve(agent_kind)` returns one or `None`. Adding a second
transport later is a data change, not a dispatch change.

### D2 · The command is a literal array, and only the process may override it

The default command is `("opencode", "acp")`. It is passed to `subprocess` as a **list**, never
through a shell (`05 §3.3`: no `shell=True`, no `os.system`, no string concatenation, no `~` /
`$VAR` / glob expansion).

Override for tests and for a second install:

```text
POSTEL_ACP_COMMAND='["/abs/path/to/fake_acp.py"]'
```

Rules:

| Input | Behaviour |
|---|---|
| unset / empty | use the registry default |
| valid JSON array of non-empty strings | use it verbatim |
| invalid JSON, empty array, non-string element | the channel is **unavailable** (`ADAPTER_UNAVAILABLE`) |

The last row is fail-closed on purpose (`06 T24`): if the override is broken we must not silently
fall back to the default, because then the user would be looking at a different agent than the
one they configured.

**Why an environment variable and not a frame parameter.** A frame that names an executable is
arbitrary code execution from the protocol boundary — `05 §3.3` forbids trusting any command that
arrives in a frame. The environment is set by whoever started the kernel, which is exactly the
trust level we need.

### D3 · Capabilities are derived from the response, and the derivation handles `{}`

Measured on OpenCode 1.19.6 (§8.1). The response carries three different shapes, all in one object:

```jsonc
"agentCapabilities": {
  "loadSession": true,                                    // boolean leaf
  "mcpCapabilities":    {"http": true, "sse": true},      // booleans under a namespace
  "promptCapabilities": {"embeddedContext": true, "image": true},
  "sessionCapabilities": {"close": {}, "fork": {}, "list": {}, "resume": {}}  // EMPTY OBJECTS
}
```

`sessionCapabilities` values are **empty option objects**, not `true`. The ACP meaning is *the key
exists ⇒ the agent supports it*; the object is where the agent would put options for that feature.
So a derivation that reads booleans would report **zero session capabilities** for a fully capable
agent. This is the one place where a plausible-looking implementation is silently wrong.

The pure function `flatten_capabilities(caps)`:

1. skip anything that is not a mapping;
2. for each key:
   - value is `True` → emit the key as-is (`loadSession`);
   - value is a mapping → strip a trailing `Capabilities` from the key to get the namespace
     (`mcpCapabilities` → `mcp`), then emit `namespace.subkey` for every subkey whose value is not
     `False`. The subkey's own value may be `true` or `{}` — both mean supported;
   - value is `False` / missing / any other type → emit nothing;
3. return the flags **sorted**, so the page and the tests agree on order.

For §8.1's response the result is exactly:

```text
loadSession, mcp.http, mcp.sse, prompt.embeddedContext, prompt.image,
session.close, session.fork, session.list, session.resume
```

Nothing in this list is written down in `src/`. It is computed. `test_flags_are_derived_not_hardcoded`
plus reverse injection R1 exist to keep it that way.

### D4 · `agent.list` starts no process; `agent.capabilities` is the handshake

| Method | Cost | What it does |
|---|---|---|
| `agent.list` | microseconds | reads the registry, `shutil.which(command[0])` to answer `available` |
| `agent.capabilities` | one process, one round-trip | spawns, `initialize`, derives flags, reaps |

Splitting them matters because `agent.list` is cheap enough to call on load, while
`agent.capabilities` must be an explicit human action — it starts a process, and on a cold install
that can take a second.

`available: false` from `agent.list` is a **static** fact (the executable is not on `PATH`); it is
not a claim that the handshake would succeed.

### D5 · `target` is reserved and refused in S9

`agent.capabilities` takes `agentKind` and an optional `target` (`07 §2.7`). In S9 a non-null
`target` returns `NOT_SUPPORTED`. Reason: the only plausible meaning at this point is "a different
executable", and that is the door D2 just closed. When S10 gives `target` the meaning "a worktree
to open the session in", it will be a path that already passed the boundary checks, and this
decision gets an addendum — not a silent reinterpretation.

### D6 · Timeout is 30s, and every failure has one stable code

| Situation | Code | Where it is caught |
|---|---|---|
| executable missing / not executable / not on `PATH` | `ADAPTER_UNAVAILABLE` | `FileNotFoundError`, `PermissionError`, `OSError` on spawn |
| process starts, no matching response within 30s | `ADAPTER_TIMEOUT` | deadline in the read loop |
| process exits or closes stdout before answering | `ADAPTER_UNAVAILABLE` | EOF before the response |
| a line arrives that is not JSON | **ignored**, keep reading | parser skips it |
| valid JSON, but no `result` / not an object / no `protocolVersion` | `INTERNAL` | shape check |

The "ignore non-JSON lines" row is not politeness: agents print banners, warnings and log lines to
stdout. A detector that treats the first non-JSON line as a protocol error would report a healthy
agent as broken. `06 T24`'s fail-closed rule applies to *missing data* (timeout, EOF, no
`protocolVersion`), not to *noise around valid data*.

`INTERNAL` for a malformed response is deliberate: `ADAPTER_UNAVAILABLE` would tell the user "you
have not installed it", which is false.

### D7 · Reaping is unconditional, and it is proven, not asserted

Order, in a `finally`:

```text
stdin.close() → terminate() → wait(5s) → kill() → wait()
```

The test asserts the pid is gone (`proc.poll() is not None`) on **every** path: success, timeout,
garbage-only output, and spawn failure (nothing to reap). "The process is usually gone" is not the
requirement; acceptance 5 says *must*, and a leaked `opencode acp` per detection is a real cost.

### D8 · The child's environment carries no credentials (`06 T22`)

`06 T22` was written for exactly this slice: *"本刀还没有 Agent，但 S9/S10 会引入。到时要确保这条。"*

The child is **not** given `os.environ.copy()`. A pure function builds the environment and drops
any variable whose name contains `TOKEN`, `KEY`, `SECRET`, `PASSWORD`, `CREDENTIAL` or `AUTH`
(case-insensitive). `SSH_AUTH_SOCK` is dropped by the same rule, which is the intended effect:
the agent has no route to push with the user's key.

`test_child_env_has_no_credentials` passes a poisoned parent environment and reads the child's own
view back (the fixture prints its environment) — it does not inspect our filter function.

### D9 · Page contract (DOM ids are contract)

A new `#agent` section, placed **after `#content` and before `#branches`**: it is a
kernel-level capability, not a property of the selected repository.

| Element | Contract |
|---|---|
| `#agent` | `data-state` ∈ `idle` / `loading` / `ready` / `error` |
| `#agent-title` | `Agent 通道` |
| `#agent-state` | one line of state text; hidden in `ready` |
| `#agent-detect` | button, label `检测 Agent`; in `error` it stays enabled and becomes the retry |
| `#agent-error` | `role="alert"`, hidden until an error |
| `#agent-detail` | hidden unless `ready` |
| `#agent-kind` / `#agent-version` / `#agent-protocol` / `#agent-elapsed` | text fields inside detail |
| `#agent-flags` | `<ul>`; one `<li>` per derived flag, sorted |

There is **no separate retry button** (acceptance 4 says "can retry", and the same button is the
retry). Adding a second button would create two code paths to the same request — the S8 lesson
(`06 T41`) about two paths diverging.

`loading` disables the button; `ready` re-enables it (a second detection is allowed and is how a
user checks after installing).

### D10 · Panel-local error text, not the global table

`ERROR_TEXT["ADAPTER_UNAVAILABLE"]` is `"内核找不到 git 命令"` — correct for S3's git path, wrong
here. Reusing it would tell the user their git installation is broken when their agent is missing.

So the page gets its own map, following S8 D8's precedent (`botCreateErrorText`):

| Code | Panel text |
|---|---|
| `ADAPTER_UNAVAILABLE` | 本机没有可用的 Agent 命令（未安装或不在 PATH 上） |
| `ADAPTER_TIMEOUT` | 握手超时（30 秒），可以重试 |
| `NOT_SUPPORTED` | 第一版不支持这个参数 |
| anything else | `ERROR_TEXT[code]` or `检测失败` |

The global `ERROR_TEXT` is not modified: other panels depend on its current values.

### D11 · A separate `api/agents.py`, not code inside `app.py`

The slice stub said "`api/app.py`（加 `agent.list` / `agent.capabilities`）". The methods are added
*to the kernel*, but the orchestration does not live in the route table: `app.py` is assembly only,
and every previous slice put its business path in its own L3 module (`repository.py`, `bots.py`,
`worktrees.py`). Adding two handlers to `app.py` would be the first exception, for no reason.

`api/agents.py` is L3, same shape as its siblings: no fastapi, raises `frames.FrameError`, receives
its collaborators by construction, exposes plain `dict → dict` / `dict → list[dict]` callables.

### D12 · Detection is not persisted

No `state.db` write, no cache. Every click re-runs the handshake (measured 0.58s, §8.1). Caching a
capability list would create the U17 failure mode again — a stored answer that silently goes stale
after an agent upgrade.

### D13 · `bot.agentKind` stays unwritable — a U item, due S10

S8's `_refuse_agent_kind` rejects every non-null `agentKind`, with the comment *"D4：S9 之前没有任何
通道被验证过"*. S9 now verifies one, so the tempting move is to open the field here.

It is not opened, because S9 ships **no UI that could choose a channel**: the create form has name
and permission only. Accepting a value the page cannot produce would be an interface ahead of any
user path. S10 (dispatch) is the first slice that must know which channel to use, and it will
either default to `acp-stdio` or add the selector. Recorded as **U18**, due S10.

## 3. Acceptance, each with the command that proves it

| # | Acceptance (what the human sees) | Command |
|---|---|---|
| 1 | With `opencode` installed: "可用", the real version, and the capability list | `uv run pytest tests/test_acp_detect.py -k real_handshake -v` |
| 2 | Not installed: "不可用" + install hint, no crash | `PATH=/nonexistent uv run pytest tests/test_acp_detect.py -k unavailable -v` |
| 3 | Capabilities come from the response, not from source | reverse injection **R1** (§7) |
| 4 | 30s with no answer: "超时", and the button can retry | `POSTEL_ACP_COMMAND='["sleep","60"]' uv run pytest tests/test_acp_detect.py -k timeout -v` |
| 5 | The child is reaped on every path | `uv run pytest tests/test_acp_detect.py -k reap -v` |

End-to-end, in a real browser:

```bash
node tools/acceptance/s9_agent.mjs      # detect → version → flags → reaped
```

The script must also assert the **negative** case (a `POSTEL_ACP_COMMAND` pointing at a missing
file → the page shows the unavailable text and no uncaught JS error), because a card that only
works when everything works is not a card.

## 4. Acceptance → test map (written before the code)

| Acceptance | Test |
|---|---|
| 1 real handshake | `test_real_handshake_reports_protocol_and_version` (skip when not installed) |
| 2 not installed | `test_missing_executable_is_unavailable`, `test_page_shows_unavailable_text` |
| 3 derived flags | `test_flags_match_the_measured_response`, `test_flags_are_derived_not_hardcoded` |
| 4 timeout | `test_no_answer_within_deadline_is_a_timeout`, `test_button_is_re_enabled_after_error` |
| 5 reaping | `test_child_is_reaped_after_success`, `test_child_is_reaped_after_timeout` |
| — noise | `test_non_json_lines_are_ignored` |
| — protocol | `test_missing_protocol_version_is_internal` |
| — security | `test_child_env_has_no_credentials` |
| — env override | `test_broken_override_is_unavailable_not_a_fallback` |
| — frame | `test_capabilities_rejects_a_target`, `test_unknown_agent_kind_is_invalid_params` |

## 5. Boundary matrix (from the protocol, not from the happy path)

| Dimension | States that must be covered |
|---|---|
| executable | on `PATH` / absolute path / missing / present but not executable |
| handshake reply | valid / non-JSON lines before it / no reply / EOF / JSON without `protocolVersion` |
| timing | answers immediately / answers near the deadline / never answers |
| channel id | known / unknown / missing / non-string |
| `target` | absent / `null` / non-null |
| process | exits after answering / stays alive after answering (must be killed) |
| page | idle → loading → ready / idle → loading → error / retry from error → ready |
| frame args | missing `agentKind` / wrong type / extra unknown field (ignored) |

## 6. Files and budget

| File | Change | Est. |
|---|---|---|
| `src/postel_studio/agent/__init__.py` | new, empty | 1 |
| `src/postel_studio/agent/acp_client.py` | new: stdio JSON-RPC + deadline + reaping | 180 |
| `src/postel_studio/agent/registry.py` | new: channel table + command resolution | 70 |
| `src/postel_studio/api/agents.py` | new, L3 orchestration | 90 |
| `src/postel_studio/api/app.py` | assembly only | 4 |
| `src/postel_studio/api/gateway.py` | two `Providers` fields + two branches | 8 |
| `src/postel_studio/web/assets.py` | card (HTML + CSS + JS) | 130 |
| `scripts/gate-layers.py` | register three new modules | 6 |

`src/` estimate: **~490 lines added, 6 files touched** (3 new, 3 modified). Ceiling is 1000 / 12
(`04 §1.2`). The estimate is written before the code, per `09 §1.5`.

Tests: `tests/test_acp_detect.py` (~230), `tests/fixtures/fake_acp.py` (~70),
`tests/test_reverse_checks.py` (+2 injections).

## 7. Reverse injections (new hard constraints only)

| # | Constraint | Inject | Must redden |
|---|---|---|---|
| R1 | capabilities are derived, never hard-coded | make `flatten_capabilities` return a fixed list | `test_flags_are_derived_not_hardcoded` |
| R2 | the child is always reaped | delete the `finally` that terminates | `test_child_is_reaped_after_timeout` |

R2 needs a target test that runs long enough to observe the leak — the timeout test with a fake
server that never answers.

## 8. What was measured before writing this spec

### 8.1 The real handshake (OpenCode 1.19.6, this machine)

Command: `opencode acp` (from `~/.local/bin/opencode`, version `1.19.6`), one request:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize",
 "params":{"protocolVersion":1,"clientCapabilities":{},
           "clientInfo":{"name":"postel-probe","version":"0.0.0"}}}
```

Raw response, first line of stdout, **0.58s** after the write:

```json
{"jsonrpc":"2.0","id":1,"result":{
  "protocolVersion":1,
  "agentCapabilities":{
    "loadSession":true,
    "mcpCapabilities":{"http":true,"sse":true},
    "promptCapabilities":{"embeddedContext":true,"image":true},
    "sessionCapabilities":{"close":{},"fork":{},"list":{},"resume":{}}},
  "authMethods":[{"description":"Run `opencode auth login` in the terminal",
                  "name":"Login with opencode","id":"opencode-login"}],
  "agentInfo":{"name":"OpenCode","version":"1.19.6"}}}
```

stderr was empty, the process was terminated by the probe, and it exited with `-15`.

Three facts this fixes in the implementation:

1. `protocolVersion` is a top-level integer, not a string and not nested.
2. `agentInfo.name` / `agentInfo.version` are what the page shows. They are **absent-tolerant**:
   an agent that omits `agentInfo` is still usable, the page just shows "未知".
3. `authMethods` exists on an unauthenticated install. Its presence is **not** an availability
   signal and is not used by S9.

### 8.2 Missing executable

```text
subprocess.Popen(["opencode","acp"], env={"PATH":"/nonexistent"})
→ FileNotFoundError: [Errno 2] No such file or directory: 'opencode'

shutil.which("opencode")                      → /home/postel/.local/bin/opencode
shutil.which("opencode", path="/nonexistent") → None
```

So `agent.list` can answer `available` without spawning, and the spawn path has exactly one
exception family to catch (`OSError`).

## 9. 05 §7 checklist (security)

- [x] New subprocess call is a **list**, no shell: D2.
- [x] No credential reaches the child: D8, asserted by reading the child's own environment.
- [x] No frame-provided path or command is executed: D2/D5.
- [x] Timeout and reaping: D6/D7.
- [x] Error codes are all from `07 §1.3` — **none added** (`ADAPTER_UNAVAILABLE`,
      `ADAPTER_TIMEOUT`, `INTERNAL`, `INVALID_PARAMS`, `NOT_SUPPORTED` all pre-exist).

## 10. Forbidden

- Hard-coding any capability value, name or count.
- `shell=True`, `os.system`, string-built commands, `~`/`$VAR`/glob expansion.
- A product name in a method name, a branch condition, or a DOM id.
- Sending a prompt, creating a session, or writing a task (that is S10).
- Writing to `state.db`.
- Leaving a child process behind on any path.
- Reusing the global `ADAPTER_UNAVAILABLE` text in the Agent card.

## 11. Definition of done

`bash scripts/check.sh all` green, `build` green, every `tools/acceptance/*.mjs` re-run (not just
the new one), the five acceptances above each backed by a pasted command tail, two reverse
injections proving R1/R2, and the PR body carrying the budget from `04 §1.1`.
