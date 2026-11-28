"""S16 · 权限门验收：真 opencode 执行前阻断 + 哨兵副作用 + 审计。

真执行用例用临时 `POSTEL_HOME` / `HOME` / `XDG_DATA_HOME`（sqlite 只读快照），不碰用户数据；
无 opencode 或无 deepseek 凭据时明确 skip，不假装拦住。纯函数与页面契约不依赖 Agent。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from postel_studio.agent import acp_client, gate
from postel_studio.domain.permission import _effect_of, rules_for
from postel_studio.storage import paths
from postel_studio.web import assets as _assets_module

REPO_ROOT = Path(__file__).resolve().parents[1]
# 从**实际加载的模块**取路径（S15 的教训）：反向注入用 PYTHONPATH 前置副本，
# 硬编码 REPO_ROOT/src 会让注入改不到测试读的文件，得到假绿。
ASSETS = Path(_assets_module.__file__)
REAL_DB = Path("/home/postel/.local/share/opencode/opencode.db")
BOT_ID = "0123456789ab/builder"
MODEL = "deepseek/deepseek-flash"
FAKE_ACP = Path(__file__).parent / "fixtures" / "fake_acp.py"


def _agent_available() -> bool:
    if shutil.which("opencode") is None or not REAL_DB.exists():
        return False
    source = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    try:
        count = source.execute(
            "SELECT count(*) FROM credential WHERE integration_id='deepseek' AND active=1"
        ).fetchone()[0]
        return int(count) > 0
    except sqlite3.Error:
        return False
    finally:
        source.close()


needs_agent = pytest.mark.skipif(
    not _agent_available(), reason="S16: opencode/deepseek unavailable"
)


def _make_repo(home: Path) -> Path:
    repo = home / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "s16@example.test"],
        ["git", "config", "user.name", "S16"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True, capture_output=True)
    bare = home / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(bare)], cwd=home, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True, capture_output=True
    )
    return repo


def _env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_content: dict[str, object]
) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    data = home / "opencode-data" / "opencode"
    data.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    target = sqlite3.connect(str(data / "opencode.db"))
    source.backup(target)
    target.close()
    source.close()
    monkeypatch.setenv("POSTEL_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "opencode-data"))
    monkeypatch.setenv(gate.CONFIG_KEY, json.dumps(config_content))


def _prompt(session: acp_client.Session, text: str) -> acp_client.SessionOutcome:
    try:
        return session.prompt(text, timeout_s=90)
    finally:
        session.close()


def _rules(permission: str) -> dict[str, object]:
    return {"$schema": "https://opencode.ai/config.json", "model": MODEL}


# ---- 纯函数：规则形状（不依赖 Agent）-------------------------------------------------


def test_rules_for_all_levels_and_fail_closed() -> None:
    assert rules_for("L0") == (("edit", "*", "deny"), ("shell", "*", "deny"))
    l1 = rules_for("L1") or ()
    assert l1[0] == ("edit", "*", "allow")
    assert ("shell", "git push *", "deny") in l1
    l2 = rules_for("L2") or ()
    assert l2[-2:] == (
        ("shell", "git push origin *", "allow"),
        ("shell", "git -C * push origin *", "allow"),
    )
    assert rules_for("L2+") == (("edit", "*", "allow"),)
    for bad in (None, "L3", "L4", "l1", 42, ""):
        assert rules_for(bad) is None


def test_rule_effect_is_last_match_wins() -> None:
    rules = rules_for("L2") or ()
    assert _effect_of(rules, "shell", "git push origin main") == "allow"
    assert _effect_of(rules, "shell", "git push /tmp/other.git main") == "deny"
    assert _effect_of(rules, "edit", "notes.md") == "allow"
    assert _effect_of(rules_for("L0") or (), "shell", "pwd") == "deny"


def test_all_push_shapes_are_denied() -> None:
    """must-fix 1：`git -C` / 路径变体 / 复合命令都不得从 deny 里漏出去。

    这些形状来自真实探针（旧规则下 `git -C <repo> push ...` 让哨兵出现、bare remote
    收到 ref）——纯函数先把每一条钉死，真执行用例再证明执行前阻断。
    """
    l1 = rules_for("L1") or ()
    shapes = (
        "git push origin main",
        "git push /tmp/other.git main",
        "git -C /tmp/repo push origin main",
        "git -C /tmp/repo push /tmp/other.git main",
        "git -C /tmp/repo push",
        "/usr/bin/git push origin main",
        "git -c core.foo=1 push origin main",
    )
    for shape in shapes:
        assert _effect_of(l1, "shell", shape) == "deny", shape
        assert _effect_of(l1, "shell", shape + " && touch sentinel.txt") == "deny", shape
    l2 = rules_for("L2") or ()
    assert _effect_of(l2, "shell", "git -C /tmp/repo push origin main") == "allow"
    assert _effect_of(l2, "shell", "git -C /tmp/repo push /tmp/other.git main") == "deny"
    assert _effect_of(l2, "shell", "/usr/bin/git push origin main") == "deny"


def test_supports_top_level_permissions() -> None:
    """D6：版本探测是纯函数，v1 与未知/缺失版本必须 fail-closed。"""
    assert acp_client.supports_top_level_permissions("2.4.2")
    assert acp_client.supports_top_level_permissions("v3.0.0")
    assert acp_client.supports_top_level_permissions("2.x")  # 只按 major 判
    for bad in ("1.9.9", "0.1", "", None, "dev"):
        assert not acp_client.supports_top_level_permissions(bad), bad


def test_unknown_permission_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTEL_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="fail-closed"):
        gate.session_factory("L3", BOT_ID)
    rows = paths.read_audit(bot_id=BOT_ID)
    assert rows and rows[-1]["effect"] == "FALLBACK"
    assert rows[-1]["source"] == "gate"
    assert rows[-1]["resource"] == "unknown-permission"


def test_v1_agent_never_starts_session_and_audits_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D6 must-fix 2：v1 Agent 不识别 permissions → 不建 session + FALLBACK 审计。

    用假 ACP 报 `1.9.9`：它把收到的每个方法写进 logfile。断言 logfile 只有
    `initialize`（`session/new` 从未发出），且 gate 写了 FALLBACK 行；异常类型让
    orchestrator 按既有 `AcpProtocolError` → `INTERNAL` 终态化，不新增错误码。
    """
    monkeypatch.setenv("POSTEL_HOME", str(tmp_path))
    logfile = tmp_path / "methods.log"
    command = [
        sys.executable,
        str(FAKE_ACP),
        "v1",
        f"--logfile={logfile}",
    ]
    factory = gate.session_factory("L1", BOT_ID)
    with pytest.raises(acp_client.AcpPermissionsUnsupportedError):
        factory(command, str(tmp_path))

    methods = logfile.read_text(encoding="utf-8").split()
    assert "initialize" in methods
    assert "session/new" not in methods, "v1 Agent 上绝不许建会话"
    rows = paths.read_audit(bot_id=BOT_ID)
    assert any(
        r["effect"] == "FALLBACK" and r["resource"] == "agent-permissions-unsupported" for r in rows
    ), rows


# ---- 真执行：哨兵文件证明"执行前"---------------------------------------------------


@needs_agent
def test_control_without_rules_creates_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(tmp_path, monkeypatch, _rules("L0"))
    repo = _make_repo(tmp_path / "home")
    session = acp_client.Session(["opencode", "acp"], str(repo))
    outcome = _prompt(
        session,
        "只使用 shell 工具执行这一条命令：touch sentinel.txt。不要用 write/edit 工具创建文件。",
    )
    assert outcome.stop_reason == "end_turn"
    assert (repo / "sentinel.txt").exists(), "没有规则时哨兵必须能生成——否则探针无效"


@needs_agent
def test_l0_write_blocked_before_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(tmp_path, monkeypatch, _rules("L0"))
    repo = _make_repo(tmp_path / "home")
    session = gate.session_factory("L0", BOT_ID)(["opencode", "acp"], str(repo))
    outcome = _prompt(
        session,
        "只使用 shell 工具执行这一条命令：touch sentinel.txt。不要用 write/edit 工具创建文件。",
    )
    assert outcome.stop_reason == "end_turn"
    assert not (repo / "sentinel.txt").exists(), "L0 写操作必须被拦在执行前"
    assert all(
        call.status != "completed" for call in outcome.tool_calls if "sentinel" in call.command
    ), outcome.tool_calls
    rows = paths.read_audit(bot_id=BOT_ID)
    assert any(r["effect"] == "DENY" and r["action"] == "shell" for r in rows)


@needs_agent
def test_l1_edit_allowed_push_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(tmp_path, monkeypatch, _rules("L1"))
    repo = _make_repo(tmp_path / "home")
    session = gate.session_factory("L1", BOT_ID)(["opencode", "acp"], str(repo))
    edited = _prompt(session, "创建 edited.txt，内容写 ok。")
    assert edited.stop_reason == "end_turn"
    assert (repo / "edited.txt").read_text(encoding="utf-8").strip() == "ok"
    # 同一个会话再试 push：哨兵只能由那条 shell 命令产生
    session = gate.session_factory("L1", BOT_ID)(["opencode", "acp"], str(repo))
    pushed = _prompt(
        session,
        "只使用 shell 工具执行这一条命令：git push origin main && touch sentinel.txt。"
        "不要用 write/edit 工具创建文件。",
    )
    assert pushed.stop_reason == "end_turn"
    assert not (repo / "sentinel.txt").exists(), "L1 不得 push（且命令未执行）"


@needs_agent
def test_l1_git_c_push_blocked_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """must-fix 1：审查实测的 `git -C <repo> push` 绕过，必须真执行证明已封死。"""
    _env(tmp_path, monkeypatch, _rules("L1"))
    repo = _make_repo(tmp_path / "home")
    bare = tmp_path / "home" / "remote.git"
    session = gate.session_factory("L1", BOT_ID)(["opencode", "acp"], str(repo))
    outcome = _prompt(
        session,
        f"只使用 shell 工具执行这一条命令：git -C {repo} push origin main && touch sentinel.txt。"
        "不要用 write/edit 工具创建文件。",
    )
    assert outcome.stop_reason == "end_turn"
    assert not (repo / "sentinel.txt").exists(), "L1 的 git -C push 必须被拦在执行前"
    refs = subprocess.run(
        ["git", "--git-dir", str(bare), "for-each-ref", "--format=%(refname)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert refs == "", f"远端不该收到任何 ref：{refs!r}"


@needs_agent
def test_l2_git_c_foreign_push_blocked_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """must-fix 1：L2 对 `git -C <repo> push <foreign>` 也必须是 fail-closed。"""
    _env(tmp_path, monkeypatch, _rules("L2"))
    repo = _make_repo(tmp_path / "home")
    foreign = tmp_path / "home" / "foreign.git"
    subprocess.run(["git", "init", "-q", "--bare", str(foreign)], check=True, capture_output=True)
    session = gate.session_factory("L2", BOT_ID)(["opencode", "acp"], str(repo))
    outcome = _prompt(
        session,
        f"只使用 shell 工具执行这一条命令：git -C {repo} push {foreign} main"
        " && touch sentinel.txt。不要用 write/edit 工具创建文件。",
    )
    assert outcome.stop_reason == "end_turn"
    assert not (repo / "sentinel.txt").exists()
    refs = subprocess.run(
        ["git", "--git-dir", str(foreign), "for-each-ref", "--format=%(refname)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert refs == "", f"外部仓库不该收到任何 ref：{refs!r}"


@needs_agent
def test_l2_origin_push_allowed_other_push_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(tmp_path, monkeypatch, _rules("L2"))
    repo = _make_repo(tmp_path / "home")
    other = tmp_path / "home" / "other.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True, capture_output=True)

    session = gate.session_factory("L2", BOT_ID)(["opencode", "acp"], str(repo))
    own = _prompt(
        session,
        "只使用 shell 工具执行这一条命令：git push origin main && touch sentinel.txt。"
        "不要用 write/edit 工具创建文件。",
    )
    assert own.stop_reason == "end_turn"
    assert (repo / "sentinel.txt").exists(), "L2 必须允许推本仓库 origin"
    (repo / "sentinel.txt").unlink()

    session = gate.session_factory("L2", BOT_ID)(["opencode", "acp"], str(repo))
    foreign = _prompt(
        session,
        f"只使用 shell 工具执行这一条命令：git push {other} main && touch sentinel.txt。"
        "不要用 write/edit 工具创建文件。",
    )
    assert foreign.stop_reason == "end_turn"
    assert not (repo / "sentinel.txt").exists(), "L2 不得 push 到其他仓库"


@needs_agent
def test_audit_records_denial_with_bot_command_and_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(tmp_path, monkeypatch, _rules("L1"))
    repo = _make_repo(tmp_path / "home")
    session = gate.session_factory("L1", BOT_ID)(["opencode", "acp"], str(repo))
    outcome = _prompt(
        session,
        "只使用 shell 工具执行这一条命令：git push origin main && touch sentinel.txt。"
        "不要用 write/edit 工具创建文件。",
    )
    rows = paths.read_audit(bot_id=BOT_ID)
    assert rows, "每次裁决都要留下审计"
    assert all(
        set(("at", "botId", "permission", "action", "resource", "effect", "source")) <= set(r)
        for r in rows
    )
    assert any(
        r["source"] == "gate" and r["effect"] == "DENY" and r["resource"] == "git push *"
        for r in rows
    )
    assert any("git push" in call.command for call in outcome.tool_calls), outcome.tool_calls
    assert all(isinstance(r["at"], float) for r in rows)


# ---- 页面契约（静态，不依赖 Agent）---------------------------------------------------


def test_page_marks_unavailable_level() -> None:
    source = ASSETS.read_text(encoding="utf-8")
    assert 'granted !== "L0"' in source
    assert 'granted !== "L2+"' in source
    assert 'badge.dataset.unavailable = "true"' in source
    assert "（不可用）" in source
    # D6：FALLBACK 审计行要真正驱动徽标不可用（不是只有静态权限值分支）
    assert 'effect === "fallback"' in source
    assert "if (hasFallback) { markPermissionUnavailable(botId); }" in source


def test_permission_badge_contract() -> None:
    source = ASSETS.read_text(encoding="utf-8")
    for token in ('id="audit"', 'id="audit-list"', 'id="audit-state"', 'request("audit.list"'):
        assert token in source, f"缺审计面板契约：{token}"
    assert "innerHTML" not in source
