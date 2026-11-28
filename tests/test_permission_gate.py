"""S16 · 权限门验收：真 opencode 执行前阻断 + 哨兵副作用 + 审计。

真执行用例用临时 `POSTEL_HOME` / `HOME` / `XDG_DATA_HOME`（sqlite 只读快照），不碰用户数据；
无 opencode 或无 deepseek 凭据时明确 skip，不假装拦住。纯函数与页面契约不依赖 Agent。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from postel_studio.agent import acp_client, gate
from postel_studio.domain.permission import _effect_of, rules_for
from postel_studio.storage import paths

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS = REPO_ROOT / "src" / "postel_studio" / "web" / "assets.py"
REAL_DB = Path("/home/postel/.local/share/opencode/opencode.db")
BOT_ID = "0123456789ab/builder"
MODEL = "deepseek/deepseek-flash"


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
    assert rules_for("L1") == (("edit", "*", "allow"), ("shell", "git push *", "deny"))
    l2 = rules_for("L2") or ()
    assert l2[-1] == ("shell", "git push origin *", "allow")
    assert rules_for("L2+") == (("edit", "*", "allow"),)
    for bad in (None, "L3", "L4", "l1", 42, ""):
        assert rules_for(bad) is None


def test_rule_effect_is_last_match_wins() -> None:
    rules = rules_for("L2") or ()
    assert _effect_of(rules, "shell", "git push origin main") == "allow"
    assert _effect_of(rules, "shell", "git push /tmp/other.git main") == "deny"
    assert _effect_of(rules, "edit", "notes.md") == "allow"
    assert _effect_of(rules_for("L0") or (), "shell", "pwd") == "deny"


def test_unknown_permission_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTEL_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="fail-closed"):
        gate.session_factory("L3", BOT_ID)
    rows = paths.read_audit(bot_id=BOT_ID)
    assert rows and rows[-1]["effect"] == "FALLBACK"
    assert rows[-1]["source"] == "gate"


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


def test_permission_badge_contract() -> None:
    source = ASSETS.read_text(encoding="utf-8")
    for token in ('id="audit"', 'id="audit-list"', 'id="audit-state"', 'request("audit.list"'):
        assert token in source, f"缺审计面板契约：{token}"
    assert "innerHTML" not in source
