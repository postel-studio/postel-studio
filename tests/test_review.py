"""S13 的验收：批准/拒绝状态机、理由落库、事件、权限提示、无合并入口。

用真 state.db（v6 迁移）+ 真实 `CandidateStore.transition`；页面断言读实际加载模块的
源码（反向注入前置 PYTHONPATH 到 src 副本，硬编码 REPO_ROOT 会打不中）。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from postel_studio.api import frames
from postel_studio.candidates import CandidateService
from postel_studio.domain.models import (
    CANDIDATE_ID_PREFIX,
    REASON_MAX_BYTES,
    Candidate,
    review_transition_ok,
)
from postel_studio.storage.db import CandidateStore, RepoStore
from postel_studio.web import assets as _assets_module

ASSETS = Path(_assets_module.__file__).read_text(encoding="utf-8")


class RecordingHub:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def publish(self, topic: str, data: dict[str, object]) -> None:
        self.events.append((topic, data))


Env = tuple[CandidateStore, RepoStore, CandidateService, RecordingHub, str]


@pytest.fixture
def review_env(
    state_home: Path,
) -> Iterator[Env]:
    """一条 local_only 候选 + 一条已拒绝候选（带理由）。"""
    from postel_studio.domain.models import Bot, Repo, Task
    from postel_studio.storage.db import BotStore, TaskStore

    candidates, repos, hub = CandidateStore(), RepoStore(), RecordingHub()
    service = CandidateService(repos, candidates, hub)
    repo_id = "c" * 12
    bots, tasks = BotStore(), TaskStore()
    repos.upsert(
        Repo(
            repo_id=repo_id,
            path=str(state_home / "review-repo"),
            default_branch="main",
            origin_url=None,
            added_at=time.time(),
        )
    )
    bots.insert(
        Bot(
            bot_id=f"{repo_id}/builder",
            repo_id=repo_id,
            name="builder",
            permission="L2",
            agent_kind=None,
            charter_rev=1,
            created_at=time.time(),
        )
    )
    for i, state in enumerate(("local_only", "rejected")):
        task_id = f"01JREVIEW{i:026d}"
        tasks.insert(
            Task(
                task_id=task_id,
                bot_id=f"{repo_id}/builder",
                trigger="manual",
                trigger_key=None,
                prompt="p",
                base_ref=None,
                state="succeeded",
                candidate_id=f"{CANDIDATE_ID_PREFIX}{task_id}",
                error_code=None,
                created_at=time.time() + i,
                started_at=None,
                ended_at=None,
            )
        )
        candidates.insert(
            Candidate(
                candidate_id=f"{CANDIDATE_ID_PREFIX}{task_id}",
                task_id=task_id,
                branch=f"bot/builder/{i}",
                base_sha="0" * 40,
                head_sha="1" * 40,
                files_changed=1,
                insertions=1,
                deletions=0,
                review_state=state,
                remote=None,
                target=None,
                created_at=time.time() + i,
                reject_reason="不需要" if state == "rejected" else None,
            )
        )
    yield candidates, repos, service, hub, f"{CANDIDATE_ID_PREFIX}01JREVIEW0{'0' * 25}"
    candidates.close()
    repos.close()
    bots.close()
    tasks.close()


def test_review_transition_matrix() -> None:
    """D1：状态机的合法/非法迁移（rejected→approved 必须被拒）。"""
    assert review_transition_ok("local_only", "approved") is True
    assert review_transition_ok("pr_open", "approved") is True
    assert review_transition_ok("local_only", "rejected") is True
    assert review_transition_ok("rejected", "approved") is False
    assert review_transition_ok("approved", "rejected") is False
    assert review_transition_ok("rejected", "rejected") is False
    assert review_transition_ok("local_only", "merged") is False  # 永不合并
    assert review_transition_ok("bogus", "approved") is False  # 坏数据 fail closed
    assert review_transition_ok(None, "approved") is False


def test_approve_legal_and_event_reaches_subscriber(review_env: Env) -> None:
    """验收 1：批准 → approved，candidate.updated 事件带 registered 载荷。"""
    candidates, _repos, service, hub, cid = review_env
    payload = service.approve({"candidateId": cid})
    assert payload["reviewState"] == "approved"
    approved = candidates.get(cid)
    assert approved is not None and approved.review_state == "approved"
    assert hub.events == [("candidate.updated", {"candidateId": cid, "reviewState": "approved"})]


def test_rejected_cannot_be_approved(review_env: Env) -> None:
    """验收 1 / D1：终态拒绝不能被批准（服务端守住，不靠 UI）。"""
    _candidates, _repos, service, _hub, _cid = review_env
    rejected_id = f"{CANDIDATE_ID_PREFIX}01JREVIEW1{'0' * 25}"
    with pytest.raises(frames.FrameError):
        service.approve({"candidateId": rejected_id})


def test_reject_reason_survives_store_reopen(review_env: Env, state_home: Path) -> None:
    """验收 3：拒绝理由随状态一起落库，关库重开还在。"""
    candidates, _repos, service, _hub, cid = review_env
    reason = "  改动没过测试，先退回  "
    payload = service.reject({"candidateId": cid, "reason": reason})
    assert payload["rejectReason"] == reason.strip()
    path = candidates.path
    candidates.close()
    reopened = CandidateStore(path)
    try:
        row = reopened.get(cid)
        assert row is not None and row.review_state == "rejected"
        assert row.reject_reason == reason.strip()
    finally:
        reopened.close()


def test_reason_is_capped_and_trimmed(review_env: Env) -> None:
    """D2：理由 trim + UTF-8 字节上限（超限 INVALID_PARAMS，不静默截断）。"""
    _candidates, _repos, service, _hub, cid = review_env
    with pytest.raises(frames.FrameError):
        service.reject({"candidateId": cid, "reason": "好" * (REASON_MAX_BYTES // 3 + 1)})
    assert service.reject({"candidateId": cid, "reason": "   "})["rejectReason"] is None


def test_transition_loser_does_not_overwrite_reason(review_env: Env) -> None:
    """D3：竞态输家不能覆盖赢家写入的理由（WHERE review_state 是真正的门）。"""
    candidates, _repos, service, _hub, cid = review_env
    first = service.reject({"candidateId": cid, "reason": "赢家理由"})
    assert first["rejectReason"] == "赢家理由"
    # 第二次直接打 store（跳过 service 的前置读），必须被 WHERE 挡住。
    assert candidates.transition(cid, "rejected", reject_reason="输家理由") is None
    winner = candidates.get(cid)
    assert winner is not None and winner.reject_reason == "赢家理由"


def test_reject_hides_from_default_list(review_env: Env) -> None:
    """验收 2：默认列表只看 local_only；已拒绝在筛选下仍可见。"""
    _candidates, _repos, service, _hub, _cid = review_env
    default = service.list({"repoId": "c" * 12, "reviewState": "local_only"})
    assert len(default) == 1 and default[0]["reviewState"] == "local_only"
    rejected = service.list({"repoId": "c" * 12, "reviewState": "rejected"})
    assert len(rejected) == 1 and rejected[0]["rejectReason"] == "不需要"


def test_no_merge_button_anywhere() -> None:
    """验收 4 / D5：页面按钮集合里没有「合并」（L4 永不开放）。"""
    labels = [
        line
        for line in ASSETS.splitlines()
        if ("<button" in line or 'type="button"' in line) and "合并" in line
    ]
    assert labels == []
    assert 'id="candidate-approve"' in ASSETS
    assert 'id="candidate-reject"' in ASSETS


def test_permission_hint_disables_button() -> None:
    """验收 5 / D4：权限不足时按钮 disabled 且有可见原因文案。"""
    assert "candidateApprove.disabled = !enough" in ASSETS
    assert "candidateReject.disabled = !enough" in ASSETS
    assert "需要 L1 以上，当前 " in ASSETS
    assert 'id="candidate-reason-note"' in ASSETS


def test_event_topic_registered() -> None:
    """D6：candidate.updated 在白名单里，且页面订阅它。"""
    from postel_studio.api import frames

    assert "candidate.updated" in frames.KNOWN_TOPICS
    assert '"candidate.updated"' in ASSETS
