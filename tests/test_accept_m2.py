"""S14 验收：`scripts/accept_m2.py` 真子进程跑（不 mock），从外面验证输出契约。

脚本自己造临时工作目录、起真内核、快照真 opencode 凭据库、建真 git 仓库、真派活
（deepseek）→ 候选 → diff → 批准。这里只从**外面**验它：退出码、十步 `✓`、
`M2 OK` / `M2 FAILED at <step>` / `SKIP M2:` 的语义、清理与"不碰真实数据"。

不做的：不解析每一步的语义（脚本自己断言）、不 mock opencode（那是 S13 单测的事）。
无 deepseek 凭据的机器上，真跑用例明确 skip（不是假装通过）——与脚本自身的 D4 一致。
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "accept_m2.py"
TIMEOUT_SECONDS = 240  # 真 opencode 一轮 ~2s + 内核启动，留足余量
WORK_DIR_LINE = re.compile(r"工作目录：(\S+)")


def _has_credential() -> bool:
    """本机有没有 active deepseek 凭据（只读 integration_id/active，不读 key）。"""
    db = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "share" / "opencode" / "opencode.db"
    if not db.is_file() or shutil.which("opencode") is None:
        return False
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM credential WHERE integration_id='deepseek' AND active=1 LIMIT 1"
                ).fetchone()
                is not None
            )
    except sqlite3.Error:
        return False


def _run(
    script: Path = SCRIPT, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
        env=env if env is not None else dict(os.environ),
    )


@pytest.mark.skipif(not _has_credential(), reason="本机没有 active deepseek 凭据 / opencode")
def test_accept_m2_script_runs_to_green() -> None:
    """`uv run python scripts/accept_m2.py` → exit 0、十步 `✓` + `M2 OK` + 清理。"""
    result = _run()
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"退出码 {result.returncode}\n{output[-4000:]}"
    for step in (
        "建临时仓库",
        "绑定仓库",
        "建 Bot",
        "分配工作区",
        "检测 Agent",
        "派活",
        "等待完成",
        "候选出现",
        "查 diff",
        "批准",
    ):
        assert f"✓ {step} (" in output, f"少了步骤 {step}\n{output[-3000:]}"
    assert "M2 OK (10/10 steps)" in output, output[-2000:]

    match = WORK_DIR_LINE.search(output)
    assert match is not None, output[-2000:]
    work = Path(match.group(1))
    assert not work.exists(), f"临时目录残留：{work}"
    assert "临时目录已删除" in output, output[-1000:]


@pytest.mark.skipif(not _has_credential(), reason="本机没有 active deepseek 凭据 / opencode")
def test_accept_m2_step_failure_names_step_and_exits_nonzero() -> None:
    """坏 Agent 命令 → `M2 FAILED at 检测 Agent` + 非零退出（D2 的失败语义）。"""
    env = dict(os.environ)
    env["POSTEL_ACP_COMMAND"] = '["no-such-agent-binary-xyz"]'  # 合法 JSON，但二进制不存在
    result = _run(env=env)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output[-3000:]
    assert "M2 FAILED at 检测 Agent" in output, output[-3000:]
    assert "M2 OK" not in output, output[-2000:]
    # 失败路径也要清理（D6）
    assert "临时目录已删除" in output, output[-1000:]


def test_accept_m2_skips_when_agent_unavailable() -> None:
    """PATH 里没有 opencode → `SKIP M2:` + exit 0，绝不输出 `M2 OK`（禁止事项 #2）。"""
    bare = REPO_ROOT / ".venv" / "bin"
    env = dict(os.environ)
    env["PATH"] = str(bare)  # python 还在（venv/bin），opencode 不在
    result = _run(env=env)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-2000:]
    assert "SKIP M2: opencode not installed" in output, output[-2000:]
    assert "M2 OK" not in output, output[-2000:]


@pytest.mark.skipif(not _has_credential(), reason="本机没有 active deepseek 凭据 / opencode")
def test_accept_m2_never_touches_real_home(tmp_path: Path) -> None:
    """真实 HOME 下的 state.db / opencode.db 不被写；快照只在临时 work 目录里。"""
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    real_state = real_home / ".postel" / "state.db"
    real_db = real_home / ".local" / "share" / "opencode" / "opencode.db"
    before = (
        real_state.stat().st_mtime_ns if real_state.exists() else None,
        real_db.stat().st_mtime_ns,
    )
    result = _run()
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-3000:]
    after = (
        real_state.stat().st_mtime_ns if real_state.exists() else None,
        real_db.stat().st_mtime_ns,
    )
    assert before == after, f"真实数据被写了：{before} → {after}"
    # 脚本打印的 work 目录已被删除；快照 db 在那底下（删除即没残留）
    match = WORK_DIR_LINE.search(output)
    assert match is not None, output[-2000:]
    assert not Path(match.group(1)).exists()


def test_accept_m2_output_contract_in_script() -> None:
    """静态守契约：脚本必须打印这些字面量（改格式会让本测试先红）。"""
    source = SCRIPT.read_text(encoding="utf-8")
    for token in (
        "工作目录：",
        "✓ ",
        "M2 OK (10/10 steps)",
        "M2 FAILED at ",
        "SKIP M2:",
        "清理：",
        "OPENCODE_CONFIG_CONTENT",
        "src.backup",  # sqlite 只读快照的字面量（D3）
        "mode=ro",
    ):
        assert token in source, f"脚本输出/行为契约缺 {token}"
    # 不得把 key 读进脚本：只看 integration_id / active，不读 value 列
    assert "integration_id='deepseek' AND active=1" in source
    assert "SELECT value" not in source and "SELECT *" not in source
