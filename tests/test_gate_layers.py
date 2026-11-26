"""S4.5 · 层依赖门禁（`scripts/gate-layers.py`）的**反向验证**（结掉 U5）。

`02-layers §4` 的规则以前只是**文档里的片段**，没人跑；这里给每条边配一个注入用例，
保证「写错了会红」而不是「文档说不会写错」。每条用例只注入一类缺陷，并断言输出里出现
该类违例的判定词（只断言非零退出会放过「因为别的原因红」的假通过）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "gate-layers.py"

CLEAN = '"""最小模块：只有 docstring，不 import 任何东西。"""\n'


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """只含**已登记**模块的最小包（基线能过）。"""
    root = tmp_path / "repo"
    package = root / "src" / "postel_studio"
    for rel in (
        "__init__.py",
        "api/__init__.py",
        "api/frames.py",
        "api/gateway.py",
        "api/repository.py",
        "web/assets.py",
    ):
        _write(package / rel, CLEAN)
    return root


def test_baseline_passes(tree: Path) -> None:
    result = _run(tree)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "层依赖门禁通过" in result.stdout


def test_unregistered_module_is_reported(tree: Path) -> None:
    """新模块不登记层 = 逃逸规则；这道门禁宁可报错也不静默放过。"""
    _write(tree / "src" / "postel_studio" / "api" / "brand_new.py", CLEAN)
    result = _run(tree)
    assert result.returncode == 1
    assert "未登记层依赖" in result.stdout, result.stdout
    assert "brand_new.py" in result.stdout


def test_frames_importing_fastapi_is_reported(tree: Path) -> None:
    """L1 是纯数据变换：连框架都不许 import。"""
    _write(tree / "src" / "postel_studio" / "api" / "frames.py", "import fastapi\n")
    result = _run(tree)
    assert result.returncode == 1
    assert "api/frames.py:1" in result.stdout, result.stdout
    assert "fastapi" in result.stdout


def test_import_form_is_checked_too(tree: Path) -> None:
    """`import subprocess`（不是 `from … import …`）同样要被抓到。"""
    _write(tree / "src" / "postel_studio" / "api" / "frames.py", "import subprocess\n")
    result = _run(tree)
    assert result.returncode == 1
    assert "subprocess" in result.stdout, result.stdout


def test_gateway_importing_app_is_reported(tree: Path) -> None:
    """T14 的边：网关不反向 import app（能力靠 Providers 注入，反向就成环）。"""
    _write(
        tree / "src" / "postel_studio" / "api" / "gateway.py", "from postel_studio.api import app\n"
    )
    result = _run(tree)
    assert result.returncode == 1
    assert "api/gateway.py" in result.stdout, result.stdout
    assert "postel_studio.api.app" in result.stdout


def test_repository_importing_fastapi_is_reported(tree: Path) -> None:
    """S3 的明确要求：业务编排层不碰框架。"""
    _write(
        tree / "src" / "postel_studio" / "api" / "repository.py",
        "from fastapi import HTTPException\n",
    )
    result = _run(tree)
    assert result.returncode == 1
    assert "fastapi" in result.stdout, result.stdout


def test_downward_import_is_allowed(tree: Path) -> None:
    """向下 import 必须过：repository（L3）用 frames（L1）是正常边。"""
    _write(
        tree / "src" / "postel_studio" / "api" / "repository.py",
        "from postel_studio.api import frames\n",
    )
    result = _run(tree)
    assert result.returncode == 0, result.stdout + result.stderr


def test_submodule_of_forbidden_package_is_caught(tree: Path) -> None:
    """前缀匹配要能抓住子模块（`postel_studio.storage.db` 被 `postel_studio.storage` 覆盖）。"""
    _write(
        tree / "src" / "postel_studio" / "api" / "gateway.py",
        "from postel_studio.storage.db import RepoStore\n",
    )
    result = _run(tree)
    assert result.returncode == 1
    assert "postel_studio.storage" in result.stdout, result.stdout


def test_missing_package_is_usage_error(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "找不到包目录" in result.stderr


def test_real_repository_is_clean() -> None:
    """当前仓库必须过——否则门禁一加进来就是红的。"""
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
