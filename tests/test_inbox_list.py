"""S12 的验收：收件箱列表的契约、筛选、排序、空态与 DOM 纪律。

S12 是纯前端切片：后端 `candidate.list` 已在 S11 支持 reviewState 过滤与倒序。
这里钉住两件事：① 服务端契约仍逐字段满足页面的渲染需求（真库）；② 页面源码
确实按 D1–D6 的形态渲染（静态断言，浏览器端由 s12_inbox.mjs 补）。
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from postel_studio.candidates import CandidateService
from postel_studio.domain.models import CANDIDATE_ID_PREFIX, Bot, Candidate, Repo
from postel_studio.storage.db import BotStore, CandidateStore, RepoStore

REPO_ROOT = Path(__file__).resolve().parents[1]
# 注入框架前置 PYTHONPATH 到 src 副本：必须从**实际加载的模块**读源码，
# 硬编码 REPO_ROOT 会让注入改不到断言目标（反向注入永远绿）。
from postel_studio.web import assets as _assets_module  # noqa: E402

ASSETS = Path(_assets_module.__file__).read_text(encoding="utf-8")
PAYLOAD_FIELDS = {
    "candidateId",
    "taskId",
    "botName",
    "branch",
    "baseSha",
    "headSha",
    "filesChanged",
    "insertions",
    "deletions",
    "reviewState",
    "remote",
    "target",
    "createdAt",
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture
def inbox_env(state_home: Path) -> Iterator[tuple[RepoStore, BotStore, CandidateStore, str, str]]:
    """一条绑定仓库 + 一个 Bot + 三状态候选（跨毫秒，createdAt 递增）。"""
    repo_path = state_home.parent / "inbox-repo"
    repo_path.mkdir()
    _git(repo_path, "init", "-q", "--initial-branch=main")
    _git(
        repo_path,
        "-c",
        "user.email=a@b",
        "-c",
        "user.name=a",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    repos, bots, candidates = RepoStore(), BotStore(), CandidateStore()
    repo = repos.upsert(
        Repo(
            repo_id="a" * 12,
            path=str(repo_path),
            default_branch="main",
            origin_url=None,
            origin_hash=None,
            added_at=time.time(),
        )
    )
    repo_id = repo.repo_id
    bots.insert(
        Bot(
            bot_id=f"{repo_id}/builder",
            repo_id=repo_id,
            name="builder",
            permission="L1",
            agent_kind=None,
            charter_rev=1,
            created_at=time.time(),
        )
    )
    from postel_studio.domain.models import Task
    from postel_studio.storage.db import TaskStore

    tasks = TaskStore()
    for i, state in enumerate(("local_only", "pushed", "pr_open")):
        task_id = f"01JINBOX{i:026d}"  # 26 chars + prefix = ULID-like unique id
        # tasks -> bots join 是 list_for_repo 的过滤路径；candidate 有 task 外键，先插 task。
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
                files_changed=i + 1,
                insertions=i + 1,
                deletions=0,
                review_state=state,
                remote=None,
                target=None,
                created_at=time.time() + i,
            )
        )
    yield repos, bots, candidates, repo_id, str(repo_path)
    for store in (repos, bots, candidates, tasks):
        store.close()


def test_payload_has_every_field_the_row_renders(
    inbox_env: tuple[RepoStore, BotStore, CandidateStore, str, str],
) -> None:
    """验收 1：帧载荷要包含行渲染需要的每个字段（bot/stat/state/time）。"""
    _repos, _bots, candidates, repo_id, _path = inbox_env
    service = CandidateService(_repos, candidates)
    rows = service.list({"repoId": repo_id})
    assert len(rows) == 3
    assert set(rows[0]) == PAYLOAD_FIELDS
    assert rows[0]["botName"] == "builder"
    assert rows[0]["reviewState"] in {"local_only", "pushed", "pr_open"}
    assert isinstance(rows[0]["createdAt"], float)


def test_review_state_filter_returns_only_matching_rows(
    inbox_env: tuple[RepoStore, BotStore, CandidateStore, str, str],
) -> None:
    """验收 3：筛选由服务端 candidate.list 完成（页面只传参数，不本地挑）。"""
    repos, _bots, candidates, repo_id, _path = inbox_env
    # D3：页面必须真的把 reviewState 传给后端（注入 S12 #3 砍掉这行时要红）。
    assert 'if (filter !== "all") { params.reviewState = filter; }' in ASSETS
    service = CandidateService(repos, candidates)
    assert len(service.list({"repoId": repo_id, "reviewState": "local_only"})) == 1
    assert len(service.list({"repoId": repo_id, "reviewState": "pushed"})) == 1
    assert len(service.list({"repoId": repo_id, "reviewState": "pr_open"})) == 1
    from postel_studio.api import frames

    with pytest.raises(frames.FrameError):
        service.list({"repoId": repo_id, "reviewState": "nonsense"})


def test_rows_are_newest_first(
    inbox_env: tuple[RepoStore, BotStore, CandidateStore, str, str],
) -> None:
    """验收 4：created_at DESC（服务端 SQL，不靠前端 reverse）。"""
    repos, _bots, candidates, repo_id, _path = inbox_env
    rows = CandidateService(repos, candidates).list({"repoId": repo_id})
    times = [float(str(row["createdAt"])) for row in rows]
    assert times == sorted(times, reverse=True)


def test_empty_repo_returns_no_rows_and_page_has_empty_state_text(state_home: Path) -> None:
    """验收 2：0 条 → 服务端 [] + 页面真实空态文案（不是空白）。"""
    repos, candidates = RepoStore(), CandidateStore()
    repo_path = state_home / "empty-repo"
    repo_path.mkdir()
    _git(repo_path, "init", "-q", "--initial-branch=main")
    _git(
        repo_path,
        "-c",
        "user.email=a@b",
        "-c",
        "user.name=a",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    repo = repos.upsert(
        Repo(
            repo_id="b" * 12,
            path=str(repo_path),
            default_branch="main",
            origin_url=None,
            origin_hash=None,
            added_at=time.time(),
        )
    )
    assert CandidateService(repos, candidates).list({"repoId": repo.repo_id}) == []
    assert "暂无待审变更" in ASSETS
    # D2：0 条时 JS 必须真的把 state 置为 empty（不能只在 CSS 里留个规则）。
    assert (
        'inboxEl.dataset.state = "empty";\n        inboxState.textContent = "暂无待审变更";'
    ) in ASSETS
    repos.close()
    candidates.close()


def test_no_new_top_level_tab_and_entry_uses_repo_row() -> None:
    """验收 6 / Q1 D1：入口在仓库条目下，不新增第 8 个一级 tab。"""
    assert 'inboxLink.className = "repo-inbox-link";' in ASSETS
    assert "变更收件箱" in ASSETS
    # 页面没有 tab bar 结构；7 个 tab 只在文档契约里，页面不得自建导航。
    assert 'id="tab-' not in ASSETS
    assert 'role="tab"' not in ASSETS


def test_row_task_id_feeds_existing_detail_loader() -> None:
    """验收 5：点击行只把 taskId 交给 S11 的 loadCandidate，不另画详情页。"""
    assert re.search(r"li\.dataset\.taskId\s*=", ASSETS)
    assert re.search(r"loadCandidate\(taskId\)", ASSETS)
    assert re.search(r"candidateEl\.scrollIntoView", ASSETS)
    assert ASSETS.count('id="candidate"') == 1  # 只有一个详情卡


def test_relative_time_boundaries() -> None:
    """D5：相对时间纯函数的四段边界与未来时间兜底。"""
    funcs = re.search(r"function relativeTime\([^)]*\)\s*\{(.*?)\n  \}", ASSETS, re.S)
    assert funcs, "relativeTime helper must exist in the page JS"
    body = funcs.group(1)
    for token in ('"刚刚"', '" 分钟前"', '" 小时前"', '" 天前"', "isFinite"):
        assert token in body
    # 前端 helper 的语义另行由 s12_inbox.mjs 在真浏览器里断言「刚刚」。


def test_filter_values_match_review_states() -> None:
    """D3：页面 <select> 的五个值与后端 REVIEW_STATES 对齐（加 all）。"""
    from postel_studio.domain.models import REVIEW_STATES

    opts = set(re.findall(r'<option value="([a-z_]+)">', ASSETS))
    state_opts = {v for v in opts if v != "all"}
    assert state_opts == set(REVIEW_STATES)
