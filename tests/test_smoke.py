"""入口的最小契约。M1 会在 `serve` 真正实现后替换掉对应断言。"""

from __future__ import annotations

import pytest

from postel_studio import __version__, main


def test_version_subcommand_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_no_subcommand_prints_help_and_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "serve" in capsys.readouterr().out


def test_serve_is_explicitly_unimplemented(capsys: pytest.CaptureFixture[str]) -> None:
    """未实现的功能必须明确失败，不能静默返回 0（否则 M1 验收会被假通过）。"""
    assert main(["serve"]) == 1
    assert "尚未实现" in capsys.readouterr().err


@pytest.mark.slow
def test_slow_marker_is_registered() -> None:
    """`--strict-markers` 下未注册的 marker 会报错，此用例证明注册生效。"""
    assert True
