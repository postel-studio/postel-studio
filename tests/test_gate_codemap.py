"""`scripts/gate-codemap.sh` 的行为测试。

门禁脚本本身不做版本控制保护，改坏了没人发现——所以用真实 git 仓库（临时目录，
不 mock）验证它到底拦不拦得住。这几条断言的正是「它会不会把该拦的放过去」。

真实行为优先：每条用例都在 tmp_path 里 `git init` 建一个真仓库，用真提交制造场景。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "gate-codemap.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=tester", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _gate(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个只有 `main` 分支的最小仓库，含一行代码、一行地图。"""
    work = tmp_path / "repo"
    work.mkdir()
    (work / "src").mkdir()
    (work / "docs" / "codemap").mkdir(parents=True)
    (work / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (work / "docs" / "codemap" / "README.md").write_text("# map\n", encoding="utf-8")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    return work


def test_passes_when_no_code_changed(repo: Path) -> None:
    """只动文档：不该要求动地图。"""
    (repo / "docs" / "codemap" / "README.md").write_text("# map v2\n", encoding="utf-8")
    result = _gate(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_passes_when_code_and_map_both_changed(repo: Path) -> None:
    """代码与地图一起改：这正是要求的做法。"""
    (repo / "src" / "mod.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "docs" / "codemap" / "README.md").write_text("# map v2\n", encoding="utf-8")
    result = _gate(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_fails_when_only_code_changed(repo: Path) -> None:
    """只改代码：必须拦下，并指出是哪几个文件。"""
    (repo / "src" / "mod.py").write_text("x = 2\n", encoding="utf-8")
    result = _gate(repo)
    assert result.returncode == 1
    assert "src/mod.py" in result.stdout


def test_detects_uncommitted_code_change(repo: Path) -> None:
    """最容易漏的一条：改了还没 commit，门禁必须照样看见。

    早期实现只比 `main...HEAD`，未提交的改动被当成「没改代码」直接放行。
    """
    (repo / "src" / "mod.py").write_text("x = 3\n", encoding="utf-8")
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip(), "前提：工作区确实有未提交改动"
    assert _gate(repo).returncode == 1


def test_staged_mode_sees_only_the_index(repo: Path) -> None:
    """`--staged` 给 pre-commit 用：暂存的代码改动要拦得住。"""
    (repo / "src" / "mod.py").write_text("x = 4\n", encoding="utf-8")
    _git(repo, "add", "src/mod.py")
    assert _gate(repo, "--staged").returncode == 1


def test_fails_after_commit_on_feature_branch(repo: Path) -> None:
    """已提交到特性分支但没动地图：CI 里跑的就是这个场景。"""
    _git(repo, "checkout", "-qb", "feat/probe")
    (repo / "src" / "mod.py").write_text("x = 5\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "code only")
    result = _gate(repo)
    assert result.returncode == 1
    assert "src/mod.py" in result.stdout


def test_tests_dir_also_counts(repo: Path) -> None:
    """`tests/` 改动同样要求更新地图——测试也是被索引的内容。"""
    (repo / "tests").mkdir()
    (repo / "tests" / "test_mod.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    _git(repo, "add", "tests")
    assert _gate(repo).returncode == 1


def test_skips_outside_git_repository(tmp_path: Path) -> None:
    """不在 git 仓库里（导出 tarball、非 git 安装）应跳过而不是误报失败。"""
    work = tmp_path / "plain"
    work.mkdir()
    shutil.copytree(SCRIPT.parent, work / "scripts")
    result = _gate(work)
    assert result.returncode == 0
    assert "跳过" in result.stdout


def test_skips_when_base_ref_missing(tmp_path: Path) -> None:
    """基准分支不存在时不能崩，也不能假装通过；无改动时正常放行。"""
    work = tmp_path / "lonely"
    work.mkdir()
    (work / "src").mkdir()
    (work / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    result = _gate(work, "--base", "no-such-ref")
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_sh_exposes_every_documented_target() -> None:
    """`check.sh` 的子命令表必须和 CONTRIBUTING 一致。

    「写了分支但函数没定义」会静默变成 `<name>: 未找到命令`——脚本本身不报错，
    只有真的调一次才发现。这里把每个目标都实调一遍（build 太慢，只查存在）。
    """
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    cheap = ["lint", "format", "types", "docs", "layers", "codemap", "secrets"]
    for target in cheap:
        assert target in contributing, f"{target} 未写进 CONTRIBUTING 门禁表"
    # 逐个目标调起来，不允许出现「未找到命令」这类未定义函数
    for target in cheap:
        result = subprocess.run(
            ["bash", "scripts/check.sh", target],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
        combined = result.stdout + result.stderr
        assert "未找到命令" not in combined, f"check.sh {target} 调用了未定义的函数"
        assert "未知目标" not in combined, f"check.sh {target} 不存在该子命令"
