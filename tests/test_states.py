"""S18：空态、错误文案与「不可达/权限不足」的用户可见契约。

这一刀把「错误码 → 文案」的唯一来源挪到 `api/errors.py`（页面靠注入），并给候选/审计/
上下文补空态。这里的断言分三类：

1. **对账**：07 §1.3 表里的码、源码里当作错误码用的常量，与 `ERROR_TEXT` 两边一致；
2. **脱敏**：`message` 里的路径/堆栈/凭据不进帧；
3. **DOM 契约**：空态节点、断线提示、权限原因、浅色主题的静态形状。

真实浏览器里的显隐与对比度由 `tools/acceptance/s18_states.mjs` 证明（T9：pytest
不假装覆盖浏览器 DOM）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from postel_studio.api import errors, frames
from postel_studio.web import assets

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = REPO_ROOT / "docs" / "design" / "07-api-contract.md"
SOURCE_ROOT = REPO_ROOT / "src" / "postel_studio"

# 空态节点：id → 必须出现的那句人话（改文案 = 改契约，下面的 DOM 测试会红）
EMPTY_STATES: dict[str, str] = {
    "repo-empty": "还没有绑定仓库",
    "branch-empty": "没有任何分支",
    "bot-empty": "还没有 Bot",
    "inbox-empty": "暂无待审变更",
    "workspace-empty": "还没有分配工作区",
    "candidate-empty": "还没有候选变更",
    "audit-empty": "还没有权限裁决记录",
    "context-empty": "没有需要注入的上下文",
}


def _page() -> str:
    return assets.render_index(
        version="0.1.2",
        api_version="1",
        state_root="/tmp/state",
        handshake_ticket="t" * 64,
        ws_path="/gateway/x",
        error_text=errors.ERROR_TEXT,
        unknown_code_text=errors.UNKNOWN_CODE_TEXT,
    )


def _documented_codes() -> set[str]:
    """07 §1.3 第一张表（协议帧错误码）里的码。进程启动码只到 stderr，不在页面里。"""
    text = CONTRACT.read_text(encoding="utf-8")
    section = text.split("#### 进程启动错误码", 1)[0]
    section = section.split("### 1.3 错误码", 1)[1]
    return set(re.findall(r"^\| `([A-Z][A-Z_]+)` \|", section, re.MULTILINE))


def _code_like_constants() -> set[str]:
    """源码里 `NAME: Final = "NAME"` 形状的常量——它们被当作错误码用（如 frames 的常量）。"""
    pattern = re.compile(r'^([A-Z][A-Z_]{3,}): Final = "\1"$', re.MULTILINE)
    found: set[str] = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        found.update(pattern.findall(path.read_text(encoding="utf-8")))
    return found


# ---------------------------------------------------------------------------
# 1. 对账：码表 = 契约 = 源码
# ---------------------------------------------------------------------------


def test_every_documented_code_has_text() -> None:
    """验收 2：07 表里的每个码都能在 `ERROR_TEXT` 里查到一句人话。"""
    documented = _documented_codes()
    assert "FRAME_TOO_LARGE" in documented, "S18 D2：FRAME_TOO_LARGE 必须登记进 07 §1.3"
    missing = sorted(documented - set(errors.ERROR_TEXT))
    assert missing == [], f"缺文案的错误码：{missing}"


def test_no_undocumented_code_has_text() -> None:
    """反向：表里不许出现 07 没登记的码（禁止事项：不得新增未登记的错误码）。"""
    extra = sorted(set(errors.ERROR_TEXT) - _documented_codes())
    assert extra == [], f"ERROR_TEXT 里有 07 表外的码：{extra}"


def test_no_undocumented_code_is_used() -> None:
    """源码里当作错误码用的常量，必须在表里（新增码必须先改 07）。"""
    used = _code_like_constants()
    assert "FRAME_TOO_LARGE" in used  # 自检：提取规则真的抓到了 frames.py 的常量
    missing = sorted(used - set(errors.ERROR_TEXT))
    assert missing == [], f"源码在用但表里没有：{missing}"


def test_every_text_is_a_readable_sentence() -> None:
    """文案非空、不是码名本身（把码贴给用户等于没有文案）。"""
    for code, text in errors.ERROR_TEXT.items():
        assert text.strip(), code
        assert text != code, code
        assert not text.startswith(code), code


# ---------------------------------------------------------------------------
# 2. 脱敏：文案与 message 都不许泄露
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(errors.ERROR_TEXT))
def test_texts_carry_no_path_stack_or_credential(code: str) -> None:
    """验收 2：文案里没有绝对路径、堆栈特征、凭据片段。"""
    text = errors.ERROR_TEXT[code]
    assert "/" not in text and "\\" not in text, text
    assert "Traceback" not in text and 'File "' not in text, text
    assert "ghp_" not in text and "Bearer " not in text, text


@pytest.mark.parametrize(
    ("raw", "forbidden", "expected"),
    [
        ("读 /home/alice/secret/repo 失败", "/home/alice", "<路径>"),
        ("C:\\Users\\alice\\repo 打不开", "alice", "<路径>"),
        ("~/work/repo 不存在", "/work/repo", "<路径>"),
        ("token=abc123def 无效", "abc123def", "token=<凭据>"),
        ("Bearer abcdefghijkl 过期", "abcdefghijkl", "<凭据>"),
        ("ghp_" + "a" * 30 + " 泄露", "a" * 30, "<凭据>"),
    ],
)
def test_message_safe_redacts_paths_and_stacks(raw: str, forbidden: str, expected: str) -> None:
    safe = errors.message_safe(raw)
    assert forbidden not in safe, safe
    assert expected in safe, safe


def test_message_safe_drops_the_traceback_tail() -> None:
    raw = (
        'Traceback (most recent call last):\n  File "/home/a/x.py", line 3, in <module>\n'
        "RuntimeError: boom"
    )
    safe = errors.message_safe(raw)
    assert "Traceback" not in safe
    assert "RuntimeError" not in safe
    assert "/home/a" not in safe


def test_message_safe_is_idempotent() -> None:
    """同一句话擦两遍必须一样（回调链上可能重复经过编码层）。"""
    once = errors.message_safe("打开 /tmp/a/b 失败：token=xyz")
    assert errors.message_safe(once) == once


def test_encode_error_frames_carry_the_sanitized_message() -> None:
    """验收 2 的服务端底线：脱敏在**编码层**做，任何调用方都绕不过去。"""
    frame = json.loads(
        frames.encode_error(
            "c1",
            frames.INTERNAL,
            "读取 /home/alice/repo 失败\nTraceback (most recent call last):\n"
            'File "/home/alice/x.py", line 1\ntoken=abcdef123',
        )
    )
    message = frame["error"]["message"]
    assert frame["error"]["code"] == frames.INTERNAL
    assert "/home/alice" not in message
    assert "Traceback" not in message
    assert "abcdef123" not in message
    assert "<路径>" in message


def test_unknown_code_falls_back_to_a_real_sentence() -> None:
    """表外的码不许答空白，也不许把服务端原文当文案。"""
    assert errors.user_text("SOMETHING_NEW") == errors.UNKNOWN_CODE_TEXT
    assert errors.UNKNOWN_CODE_TEXT.strip()
    page = _page()
    assert f'var UNKNOWN_CODE_TEXT = "{errors.UNKNOWN_CODE_TEXT}";' in page
    assert "return ERROR_TEXT[code] || UNKNOWN_CODE_TEXT;" in page


# ---------------------------------------------------------------------------
# 3. DOM 契约：空态 / 不可达 / 权限原因 / 浅色主题
# ---------------------------------------------------------------------------


def test_every_panel_declares_an_empty_state() -> None:
    """验收 1：八个面板各有自己的空态节点，且文案不是空白。"""
    page = _page()
    for node_id, phrase in EMPTY_STATES.items():
        assert f'id="{node_id}"' in page, node_id
        line = next(line for line in page.splitlines() if f'id="{node_id}"' in line)
        assert phrase in line, (node_id, line)


def test_empty_states_stay_out_of_idle_and_loading() -> None:
    """S3 D1 的纪律：没选仓库/Bot 时不显示空态（"没选"≠"没有"）。"""
    page = _page()
    assert 'candidateEmpty.hidden = state !== "empty";' in page
    assert 'auditEmpty.hidden = state !== "empty";' in page
    assert 'contextEmpty.hidden = state !== "empty";' in page


def test_candidate_audit_context_reach_an_explicit_empty_state() -> None:
    """空态必须真的可达：三条加载路径在 0 条时都落到 `empty`，而不是留一段空白。"""
    page = _page()
    assert 'resetCandidate("empty", "");' in page
    assert 'resetAudit(rows.length === 0 ? "empty" : "ready",' in page
    assert 'if (sources.length === 0) { resetContext("empty", ""); return; }' in page


def test_page_declares_unreachable_hint() -> None:
    """验收 3：连不上时出现「内核不可达」+ 重连按钮，且不白屏（页面是静态渲染的）。"""
    page = _page()
    assert 'id="status-hint"' in page
    assert 'var UNREACHABLE_TEXT = "已断开 · 内核不可达";' in page
    assert 'var UNREACHABLE_HINT = "内核不可达：确认内核进程还在，然后点「重连」。"' in page
    assert 'setState("disconnected", UNREACHABLE_TEXT, UNREACHABLE_HINT);' in page
    # 连上后必须清掉上一次的原因，否则「不可达」会陪着一次成功的连接
    assert 'statusHint.textContent = ""; statusHint.hidden = true;' in page
    # 重连按钮的显隐只看 disconnected
    assert 'button.style.display = state === "disconnected" ? "inline-block" : "none";' in page


def test_page_shows_permission_reason_instead_of_silent_disable() -> None:
    """验收 4：L0 派活被拦时同屏给原因（不是灰一个按钮了事）。"""
    page = _page()
    assert 'id="task-permission-note"' in page
    assert 'aria-describedby="task-permission-note"' in page
    assert "function updateTaskPermissionNote()" in page
    assert (
        "taskPermissionNote.textContent = blocked ? "
        'String(ERROR_TEXT.PERMISSION_DENIED || "") : "";' in page
    )
    assert 'var blocked = permission === "L0";' in page


def test_audit_callback_uses_err_data_shape() -> None:
    """D9：`request` 的回调是 `(err, data)`；写成 `(frame)` 时审计永远答「读取审计失败」。"""
    page = _page()
    assert 'request("audit.list", { botId: botId }, function (err, data) {' in page
    assert "function (frame)" not in page
    assert 'resetAudit("error", "审计读取失败（" + errorText(err) + "）");' in page


def test_page_declares_light_theme() -> None:
    """验收 5：页面有两套 token，浅色由 prefers-color-scheme 覆盖（不是只写一套暗色）。"""
    page = _page()
    assert "@media (prefers-color-scheme: light)" in page
    assert "--bg: #0f1115" in page  # 暗色基值
    assert "--bg: #ffffff" in page  # 浅色覆盖
    assert "background: var(--bg); color: var(--text);" in page
    # 页面里不许再有第二份"只服务暗色"的硬编码正文色
    assert "color: #e6e9ef" not in page


def test_page_gets_error_text_from_the_single_source() -> None:
    """D1：页面里的表是注入的，`web/assets.py` 自己不再持有 `ERROR_TEXT`。"""
    page = _page()
    assert '"NOT_A_REPO": "不是 git 仓库"' in page
    assert '"FRAME_TOO_LARGE": "请求太大（单帧超过 1 MiB），拆小后再试"' in page
    assert not hasattr(assets, "ERROR_TEXT")
    web_source = (SOURCE_ROOT / "web" / "assets.py").read_text(encoding="utf-8")
    assert "ERROR_TEXT: Final" not in web_source


def test_every_element_id_looked_up_exists_in_the_page() -> None:
    """页面脚本 `getElementById` 查的每个 id 都必须真的在 HTML 里。

    这条是 S18 的学费：`candidateEmpty` 声明漏了，页面顶层抛 `ReferenceError`，
    整个 WebSocket 连接没建立——pytest 全绿，只有浏览器验收能看见（T9）。
    """
    page = _page()
    looked_up = set(re.findall(r'getElementById\("([^"]+)"\)', page))
    declared = set(re.findall(r'id="([^"]+)"', page))
    missing = sorted(looked_up - declared)
    assert missing == [], f"脚本查了不存在的 id：{missing}"


def test_error_text_json_cannot_close_the_script_tag() -> None:
    """注入的是静态中文，但 `</script>` 逃逸的保险不该省（成本为零）。"""
    encoded = assets.error_text_json({"X": "</script><script>alert(1)</script>"})
    assert "</script>" not in encoded
    assert "<\\/script>" in encoded
