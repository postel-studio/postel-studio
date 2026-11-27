"""候选变更（S11）：把 Bot 工作区里真实发生的改动固化成一条可审查的候选。

两个职责，都只依赖向下层（`agent/` 不需要，`api/*` handler 一个都不 import——D13
的同一纪律）：

1. `CandidateService.emerge`：读 HEAD 快照 → 必要时 commit → 建候选分支 → 落一行 →
   交给 orchestrator 原子写 `tasks.candidate_id`。git 写操作全部在 `gitops/worktrees.py`
   的 S11 D5 允许清单里；这里没有 `reset --hard` / `clean` / `--force`。
2. `CandidateService.list`：`candidate.list` 的帧载荷（camelCase 只在边界发生）。

失败语义：候选**创建**过程中的任何 git/DB 失败抛普通 `RuntimeError`，由 orchestrator 的
`except Exception` 翻成任务 `failed` + `INTERNAL`（S10 D15：任务确实发生了，不能回帧说"没派活"）。
查询路径的坏参数才抛 `frames.FrameError`（稳定码，07 §1.3）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Final, Protocol

from postel_studio.api import frames
from postel_studio.domain.models import (
    CANDIDATE_ID_PREFIX,
    REVIEW_STATES,
    Candidate,
    Task,
    candidate_branch,
    ulid,
)
from postel_studio.gitops import repo as gitops_repo
from postel_studio.gitops import worktrees
from postel_studio.storage.db import CandidateStore, RepoStore

REPO_NOT_BOUND: Final = "REPO_NOT_BOUND"


class CandidateMaker(Protocol):
    """orchestrator 看到的候选能力（测试用假实现替换，不碰真 git）。"""

    def snapshot_base(self, worktree_path: str) -> str: ...

    def emerge(
        self, *, task: Task, repo_id: str, worktree_path: str, base_sha: str
    ) -> Candidate | None: ...

    def discard(self, candidate_id: str) -> None: ...


class CandidateService:
    """生产实现：`RepoStore` 解析允许根，`CandidateStore` 落库。"""

    def __init__(self, repos: RepoStore, candidates: CandidateStore) -> None:
        self._repos = repos
        self._candidates = candidates

    # -- 候选产出（worker 线程） ----------------------------------------------
    def snapshot_base(self, worktree_path: str) -> str:
        """任务开始前的 HEAD（S11 D2）：复用工作区时它是上一任务候选的 commit。"""
        return worktrees.head_commit(Path(worktree_path))

    def emerge(
        self, *, task: Task, repo_id: str, worktree_path: str, base_sha: str
    ) -> Candidate | None:
        """有改动则固化并返回 `Candidate`；"无事可做"回 `None`（D3，不是失败）。"""
        repo = self._repos.get(repo_id)
        if repo is None:
            raise RuntimeError("找不到这个仓库")
        root = Path(repo.path)
        path = Path(worktree_path)
        dirty = worktrees.is_dirty(path)
        head = worktrees.head_commit(path)
        if not dirty and head == base_sha:
            return None
        if dirty:
            # 消息只含内核生成的 task_id，没有用户文本（D1/D4）。
            head = worktrees.commit_all(path, f"candidate {task.task_id}")
        if head == base_sha:
            return None
        stat = gitops_repo.diff_stat(root, base_sha, head)
        if stat is None or stat.files_changed == 0:
            return None  # Agent 自己提交了但内容与 base 相同：无事可做
        bot_name = task.bot_id.rsplit("/", 1)[-1]
        branch = candidate_branch(bot_name, task.task_id)
        if worktrees.branch_exists(root, branch):  # ULID 碰撞才可能，绝不覆盖
            raise RuntimeError(f"候选分支已存在：{branch}")
        worktrees.create_branch(root, branch, head)
        candidate = Candidate(
            candidate_id=f"{CANDIDATE_ID_PREFIX}{ulid()}",
            task_id=task.task_id,
            branch=branch,
            base_sha=base_sha,
            head_sha=head,
            files_changed=stat.files_changed,
            insertions=stat.insertions,
            deletions=stat.deletions,
            review_state="local_only",
            remote=None,
            target=None,
            created_at=time.time(),
        )
        self._candidates.insert(candidate)
        return candidate

    def discard(self, candidate_id: str) -> None:
        """transition 输掉取消竞态时，删掉本 worker 刚插入的那一行（D6）。"""
        self._candidates.discard(candidate_id)

    # -- 帧查询 ---------------------------------------------------------------
    def list(self, params: dict[str, object]) -> list[dict[str, object]]:
        """`candidate.list`（07 §2.5）：按 repoId 列、按 taskId 取单条详情。"""
        repo_id = params.get("repoId")
        if not isinstance(repo_id, str) or not repo_id:
            raise frames.FrameError(frames.INVALID_PARAMS, "repoId 参数缺失或不是字符串")
        if self._repos.get(repo_id) is None:
            raise frames.FrameError(REPO_NOT_BOUND, "仓库未绑定")
        task_id = params.get("taskId")
        if task_id is not None and (not isinstance(task_id, str) or not task_id):
            raise frames.FrameError(frames.INVALID_PARAMS, "taskId 参数不合法")
        review_state = params.get("reviewState")
        if review_state is not None and review_state not in REVIEW_STATES:
            raise frames.FrameError(frames.INVALID_PARAMS, "reviewState 参数不合法")
        rows = self._candidates.list_for_repo(
            repo_id,
            review_state=str(review_state) if review_state is not None else None,
            task_id=task_id,
        )
        return [self._payload(candidate) for candidate in rows]

    @staticmethod
    def _payload(candidate: Candidate) -> dict[str, object]:
        """`Candidate` → 帧载荷（camelCase 只在边界发生，06 §2.4 的字段表）。"""
        parts = candidate.branch.split("/")
        return {
            "candidateId": candidate.candidate_id,
            "taskId": candidate.task_id,
            "botName": parts[1] if len(parts) >= 3 else "",
            "branch": candidate.branch,
            "baseSha": candidate.base_sha,
            "headSha": candidate.head_sha,
            "filesChanged": candidate.files_changed,
            "insertions": candidate.insertions,
            "deletions": candidate.deletions,
            "reviewState": candidate.review_state,
            "remote": candidate.remote,
            "target": candidate.target,
            "createdAt": candidate.created_at,
        }
