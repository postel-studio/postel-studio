#!/usr/bin/env python3
"""浏览器验收脚本的 Agent 环境注入：输出 deepseek 可用的 env JSON。

真 opencode 的免费模型额度会耗尽（06 §U21）；浏览器脚本把 HOME 指向临时目录，
opencode 读不到用户真实凭据。这里像 S13/S14 的 pytest 一样做**只读 SQLite 快照**
（`mode=ro` + `Connection.backup`）到临时目录，让内核子进程用 deepseek 真跑。

用法：`python agent_env.py <temp-home-dir>` → stdout 一行 JSON：
  {"XDG_DATA_HOME": "...", "OPENCODE_CONFIG_CONTENT": "..."}
没有 opencode / 没有 active deepseek 凭据 → 输出 `{}`（脚本照旧跑，会走免费模型并
在额度耗尽时明确失败——绝不假装成功）。key 只由 opencode 从快照读，本脚本不读 key 值。
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: agent_env.py <temp-home-dir>", file=sys.stderr)
        return 2
    if shutil.which("opencode") is None:
        print("{}")
        return 0
    real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "share" / "opencode"
    if not (real / "opencode.db").is_file():
        print("{}")
        return 0
    work = Path(sys.argv[1])
    (work / "opencode-data" / "opencode").mkdir(parents=True, exist_ok=True)
    dest_dir = work / "opencode-data" / "opencode"
    try:
        src = sqlite3.connect(f"file:{real / 'opencode.db'}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(str(dest_dir / "opencode.db"))
            try:
                src.backup(dst)
                row = dst.execute(
                    "SELECT 1 FROM credential WHERE integration_id='deepseek' AND active=1 LIMIT 1"
                ).fetchone()
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error:
        print("{}")
        return 0
    if row is None:
        print("{}")
        return 0
    print(
        json.dumps(
            {
                "XDG_DATA_HOME": str(work / "opencode-data"),
                "OPENCODE_CONFIG_CONTENT": json.dumps({"model": "deepseek/deepseek-flash"}),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
