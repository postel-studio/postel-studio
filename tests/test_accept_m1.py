"""D11 第 15 行：M1 全链路验收脚本（真子进程跑 `scripts/accept_m1.py`，不 mock）。

脚本自己造临时工作目录、起真内核子进程、建真 git 仓库（D10 的硬约束）。这里只从**外面**
验它：退出码 0、6 个 `PASS S` 与 `M1 OK` 都在、跑完临时 `POSTEL_HOME` 没有残留。

不做的事（别假装）：不解析每一步的语义、不断言内核 pid——那些由脚本自己的断言与
`tests/test_bot_charter.py` 的端到端用例分别覆盖。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "accept_m1.py"
# D10：脚本要能被当子进程跑（超时 120s）
TIMEOUT_SECONDS = 120


def test_accept_m1_script_runs_to_green() -> None:
    """`uv run python scripts/accept_m1.py` → exit 0、输出含 6 个 `PASS S` + `M1 OK`。"""
    assert SCRIPT.is_file(), SCRIPT
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
        env=dict(os.environ),
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"退出码 {result.returncode}\n{output[-4000:]}"
    for step in ("S1", "S2", "S3", "S4", "S5", "S6"):
        assert f"PASS {step} " in output, f"少了 {step}\n{output[-3000:]}"
    assert "PASS 家具" in output
    assert "M1 OK（6/6 步 + 家具）" in output, output[-2000:]

    # D11 第 15 行后半句：跑完不残留 POSTEL_HOME（脚本临时目录必须已被删掉）
    match = re.search(r"工作目录：(\S+)", output)
    assert match is not None, output[-2000:]
    work = Path(match.group(1))
    assert not work.exists(), f"临时目录残留：{work}"
    assert "临时目录已删除" in output, output[-1000:]
