#!/usr/bin/env python3
"""s15_recovery.mjs 的脚手架：在指定 state.db 里造一个 running 任务（内核已起过、schema 已建）。

只被浏览器验收脚本调用；用真 store 写行，不直接拼 SQL（外键/状态机由生产代码守门）。
命令行：`python s15_seed.py <state.db> <repo_id> <bot_id>`
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from postel_studio.domain.models import Bot, Permission, Repo, Task
from postel_studio.storage.db import BotStore, RepoStore, TaskStore


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: s15_seed.py <state.db> <repo_id> <bot_id>", file=sys.stderr)
        return 2
    db = Path(sys.argv[1])
    repo_id, bot_id = sys.argv[2], sys.argv[3]
    now = time.time()
    repos = RepoStore(db)
    bots = BotStore(db)
    tasks = TaskStore(db)
    try:
        repos.upsert(
            Repo(
                repo_id=repo_id,
                path=str(db.parent / "repo"),
                origin_url=None,
                default_branch="main",
                added_at=now,
                origin_hash=None,
            )
        )
        bots.insert(
            Bot(
                bot_id=bot_id,
                repo_id=repo_id,
                name=bot_id.split("/")[-1],
                permission=Permission.L1,
                agent_kind=None,
                charter_rev=0,
                created_at=now,
            )
        )
        tasks.insert(
            Task(
                task_id="a" * 26,
                bot_id=bot_id,
                trigger="manual",
                trigger_key=None,
                prompt="seed running task",
                base_ref=None,
                state="running",
                candidate_id=None,
                error_code=None,
                created_at=now,
                started_at=now,
                ended_at=None,
            )
        )
        print("seeded running task aaaaaaaaaaaaaaaaaaaaaaaaaa")
    finally:
        tasks.close()
        bots.close()
        repos.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
