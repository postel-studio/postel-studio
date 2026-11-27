"""S11 的验收：任务成功后，工作区里真实发生的事变成一条有 diff 的候选。

用真 git linked worktree + 真 state.db；假的是 ACP 会话（不启 opencode），它在 `prompt()`
里写文件，模拟 Agent 的产出。真 opencode 的整条路径由 s11 浏览器验收覆盖。
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from postel_studio.agent import acp_client
from postel_studio.api import gateway
from postel_studio.api.bots import BotService
from postel_studio.api.repository import RepositoryService
from postel_studio.api.worktrees import WorktreeService
from postel_studio.candidates import CandidateService
from postel_studio.domain.models import (
    CANDIDATE_ID_PREFIX,
    Candidate,
    Task,
    candidate_branch,
)
from postel_studio.orchestrator import EventHub, Orchestrator
from postel_studio.storage.db import (
    BotStore,
    CandidateStore,
    LeaseStore,
    RepoStore,
    TaskStore,
)

PRESET: Final = "builder"
WAIT_S: Final = 30.0


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "user.name=test",
        "init",
        "-q",
        "--initial-branch=main",
        str(path),
    )
    _git(
        "-C",
        str(path),
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "user.name=test",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    return path


class WritingSession:
    """假会话：按 `files` 在工作区写文件；`commit` 模拟 Agent 自己提交。"""

    def __init__(
        self, command: tuple[str, ...], cwd: str, files: dict[str, str], *, commit: bool
    ) -> None:
        self.cwd = cwd
        self.files = files
        self.commit = commit
        self.closed = False

    def prompt(self, text: str, *, timeout_s: float | None = None) -> acp_client.SessionOutcome:
        for name, content in self.files.items():
            target = Path(self.cwd) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        if self.commit:
            _git("-C", self.cwd, "add", "-A")
            _git(
                "-C",
                self.cwd,
                "-c",
                "user.email=agent@example.invalid",
                "-c",
                "user.name=agent",
                "commit",
                "-q",
                "-m",
                "agent commit",
            )
        return acp_client.SessionOutcome(stop_reason="end_turn", updates=0)

    def cancel(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class Env:
    def __init__(
        self,
        tmp_path: Path,
        stores: tuple[RepoStore, BotStore, TaskStore, LeaseStore],
        candidates: CandidateStore,
        repo: Path,
        repo_id: str,
        bot_id: str,
    ) -> None:
        self.repos, self.bots, self.tasks, self.leases = stores
        self.candidates = candidates
        self.repo = repo
        self.repo_id = repo_id
        self.bot_id = bot_id
        self.worktrees = WorktreeService(self.repos, self.bots)
        self.service = CandidateService(self.repos, candidates)
        self.hub = EventHub()
        self.sessions: list[WritingSession] = []

    def run_task(
        self, files: dict[str, str], *, commit: bool = False
    ) -> tuple[Task, Candidate | None]:
        def factory(command: tuple[str, ...], cwd: str) -> WritingSession:
            session = WritingSession(command, cwd, files, commit=commit)
            self.sessions.append(session)
            return session

        orchestrator = Orchestrator(
            self.bots,
            self.tasks,
            self.leases,
            self.worktrees,
            self.service,
            self.hub,
            session_factory=factory,  # type: ignore[arg-type]
        )
        payload = orchestrator.dispatch({"botId": self.bot_id, "prompt": "make a change"})
        task_id = str(payload["taskId"])
        final = self.wait_task(task_id)
        return final, self.candidates.for_task(task_id)

    def wait_task(self, task_id: str) -> Task:
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            task = self.tasks.get(task_id)
            if task is not None and task.state in ("succeeded", "failed", "cancelled"):
                return task
            time.sleep(0.01)
        raise AssertionError(f"任务 {task_id} 没有在 {WAIT_S}s 内终态化")


@pytest.fixture
def env(state_home: Path, tmp_path: Path) -> Iterator[Env]:
    with (
        RepoStore() as repos,
        BotStore() as bots,
        TaskStore() as tasks,
        LeaseStore() as leases,
        CandidateStore() as candidates,
    ):
        repo = _init_repo(tmp_path / "proj")
        payload = RepositoryService(repos).bind({"path": str(repo)})
        repo_id = str(payload["repoId"])
        BotService(bots, repos).create({"repoId": repo_id, "name": PRESET})
        yield Env(
            tmp_path, (repos, bots, tasks, leases), candidates, repo, repo_id, f"{repo_id}/{PRESET}"
        )


# ---------------------------------------------------------------------------
# 验收 1：有改动 → 候选落盘 + 分支真存在 + stat 与 git 一致
# ---------------------------------------------------------------------------


def test_success_with_changes_creates_candidate(env: Env) -> None:
    final, candidate = env.run_task({"hello.txt": "hello\n"})
    assert final.state == "succeeded", final.error_code
    assert final.candidate_id is not None
    assert candidate is not None
    assert candidate.candidate_id.startswith(CANDIDATE_ID_PREFIX)
    assert candidate.branch == candidate_branch(PRESET, final.task_id)
    assert candidate.review_state == "local_only"
    assert (candidate.files_changed, candidate.insertions, candidate.deletions) == (1, 1, 0)
    assert (
        _git(
            "-C",
            str(env.repo),
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{candidate.branch}",
        )
        == candidate.head_sha
    )
    shortstat = _git(
        "-C", str(env.repo), "diff", "--shortstat", f"{candidate.base_sha}...{candidate.head_sha}"
    )
    assert "1 file changed, 1 insertion(+)" in shortstat


# ---------------------------------------------------------------------------
# 验收 2：分支名派生，永远不含用户文本
# ---------------------------------------------------------------------------


def test_candidate_branch_name_is_predictable_and_sanitized() -> None:
    assert candidate_branch("builder", "01JABCDEF0123456789") == "bot/builder/23456789"
    bad = candidate_branch("Bäd Name!", "01JABCDEF")
    assert bad == "bot/b-d-name-/1jabcdef"
    assert " " not in bad and "!" not in bad and "ä" not in bad


# ---------------------------------------------------------------------------
# 验收 3：无改动仍是 succeeded，没有候选也没有分支
# ---------------------------------------------------------------------------


def test_no_change_task_stays_succeeded_without_candidate(env: Env) -> None:
    final, candidate = env.run_task({})
    assert final.state == "succeeded", final.error_code
    assert final.candidate_id is None
    assert candidate is None
    assert (
        _git("-C", str(env.repo), "branch", "--list", candidate_branch(PRESET, final.task_id)) == ""
    )


# ---------------------------------------------------------------------------
# 验收 4：候选 diff 复用 repo.diff，与 git 的 --shortstat 一致
# ---------------------------------------------------------------------------


def test_candidate_diff_matches_git_shortstat(env: Env) -> None:
    _final, candidate = env.run_task({"a.txt": "one\n", "b.txt": "two\n"})
    assert candidate is not None
    payload = RepositoryService(env.repos).diff(
        {"repoId": env.repo_id, "base": candidate.base_sha, "head": candidate.head_sha}
    )
    assert payload["stat"] == {"filesChanged": 2, "insertions": 2, "deletions": 0}
    patch = payload["patch"]
    assert isinstance(patch, str)
    assert "+one" in patch and "+two" in patch

    # 帧往返：candidate.list 的载荷是 camelCase，repoId 缺失是稳定码。
    providers = gateway.Providers(
        health=dict,
        repo_bind=lambda p: p,
        repo_list=list,
        repo_branches=lambda p: [],
        repo_diff=lambda p: p,
        bot_list=lambda p: [],
        bot_create=lambda p: p,
        bot_set_permission=lambda p: p,
        memory_charter_get=lambda p: p,
        worktree_acquire=lambda p: p,
        worktree_list=lambda p: [],
        worktree_release=lambda p: p,
        agent_list=list,
        agent_capabilities=lambda p: p,
        task_dispatch=lambda p: p,
        task_status=lambda p: p,
        task_latest=lambda p: None,  # S15
        task_cancel=lambda p: p,
        candidate_list=env.service.list,
        candidate_approve=env.service.approve,
        candidate_reject=env.service.reject,
    )
    rows = gateway.dispatch("candidate.list", {"repoId": env.repo_id}, providers)
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0]["branch"] == candidate.branch
    assert rows[0]["reviewState"] == "local_only"
    assert rows[0]["insertions"] == 2


# ---------------------------------------------------------------------------
# 验收 5：落盘后重开还在
# ---------------------------------------------------------------------------


def test_candidate_row_survives_store_reopen(env: Env) -> None:
    final, candidate = env.run_task({"hello.txt": "hello\n"})
    assert candidate is not None
    with CandidateStore(env.candidates.path) as reopened:
        assert reopened.for_task(final.task_id) == candidate
        rows = reopened.list_for_repo(env.repo_id)
        assert [row.candidate_id for row in rows] == [candidate.candidate_id]


# ---------------------------------------------------------------------------
# D2：复用工作区时第二个任务的 diff 只含第二个任务的文件
# ---------------------------------------------------------------------------


def test_second_task_on_reused_worktree_diff_is_isolated(env: Env) -> None:
    _first_task, first = env.run_task({"first.txt": "one\n"})
    assert first is not None
    _second_task, second = env.run_task({"second.txt": "two\n"})
    assert second is not None
    assert second.base_sha == first.head_sha, "base 必须是任务开始时的 HEAD，不是 baseRef"
    stat = _git(
        "-C", str(env.repo), "diff", "--shortstat", f"{second.base_sha}...{second.head_sha}"
    )
    assert "1 file changed, 1 insertion(+)" in stat
    assert "first.txt" not in _git(
        "-C", str(env.repo), "diff", "--name-only", f"{second.base_sha}...{second.head_sha}"
    )


def test_agent_committed_changes_still_produce_candidate(env: Env) -> None:
    """Agent 自己提交了（工作区干净）也算改动——HEAD 快照会变。"""
    _final, candidate = env.run_task({"committed.txt": "ok\n"}, commit=True)
    assert candidate is not None and candidate.files_changed == 1
    # Agent 自己提交后工作区是干净的，但 HEAD 变了——D2 的快照仍能看见改动。
    worktree = Path(env.candidates.path).parent / "worktrees" / env.repo_id / PRESET
    assert worktree.is_dir()
    assert _git("-C", str(worktree), "status", "--porcelain") == ""


# ---------------------------------------------------------------------------
# D8：candidate.proposed 只给订阅者；D6：transition 输掉竞态时补偿删除
# ---------------------------------------------------------------------------


def test_candidate_proposed_reaches_only_subscribers(env: Env) -> None:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    queue: asyncio.Queue[tuple[str, dict[str, object]]] = asyncio.Queue()
    env.hub.subscribe(frozenset({"task.updated"}), queue, loop)  # 没订 candidate.proposed
    candidate_queue: asyncio.Queue[tuple[str, dict[str, object]]] = asyncio.Queue()
    env.hub.subscribe(frozenset({"candidate.proposed"}), candidate_queue, loop)
    try:
        final, candidate = env.run_task({"hello.txt": "hello\n"})
        assert candidate is not None
        assert asyncio.run_coroutine_threadsafe(queue.get(), loop).result(timeout=WAIT_S)[0] == (
            "task.updated"
        )
        topic, data = asyncio.run_coroutine_threadsafe(candidate_queue.get(), loop).result(
            timeout=WAIT_S
        )
        assert topic == "candidate.proposed"
        assert data == {"candidateId": candidate.candidate_id, "taskId": final.task_id}
        assert candidate_queue.empty()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def test_transition_loser_discards_just_inserted_candidate(env: Env) -> None:
    """cancel 抢先时只允许留下 cancelled 任务，不能留下指向它的候选。"""

    class FixedMaker:
        """把刚插入的候选交给 orchestrator，逼它走 transition 输家的分支。"""

        def snapshot_base(self, worktree_path: str) -> str:
            return "0" * 40

        def emerge(self, **kwargs: object) -> Candidate:
            return candidate

        def discard(self, candidate_id: str) -> None:
            env.candidates.discard(candidate_id)

    # 先构造 orchestrator：它的 mark_interrupted() 会在测试插入任务之前跑完，
    # 否则新建的任务会被当成"上次内核留下的活跃任务"标掉（S10 D7 的既定行为）。
    orchestrator = Orchestrator(
        env.bots,
        env.tasks,
        env.leases,
        env.worktrees,
        FixedMaker(),
        env.hub,
    )
    task_id = "01JTESTCANDIDATE0000000000000"
    env.tasks.insert(
        Task(
            task_id=task_id,
            bot_id=env.bot_id,
            trigger="manual",
            trigger_key=None,
            prompt="make a change",
            base_ref=None,
            state="queued",
            candidate_id=None,
            error_code=None,
            created_at=time.time(),
            started_at=None,
            ended_at=None,
        )
    )
    running = env.tasks.transition(task_id, "running")
    assert running is not None
    candidate = Candidate(
        candidate_id=f"{CANDIDATE_ID_PREFIX}test",
        task_id=task_id,
        branch=candidate_branch(PRESET, task_id),
        base_sha="0" * 40,
        head_sha="1" * 40,
        files_changed=1,
        insertions=1,
        deletions=0,
        review_state="local_only",
        remote=None,
        target=None,
        created_at=time.time(),
    )
    env.candidates.insert(candidate)

    lost = env.tasks.transition(task_id, "cancelled")  # cancel 抢先
    assert lost is not None
    # 直接调私有方法才能不启 worker 就复现竞态分支。
    final = orchestrator._succeed_with_candidate(
        running, env.repo_id, str(Path(env.candidates.path).parent), "0" * 40
    )
    assert final.state == "cancelled"
    assert env.candidates.get(candidate.candidate_id) is None
    assert env.candidates.for_task(task_id) is None
