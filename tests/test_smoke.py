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


def test_serve_rejects_out_of_range_port(capsys: pytest.CaptureFixture[str]) -> None:
    """非法参数必须显式失败（非 0 且给稳定错误码），不能挂起或静默成功。

    S1 已实现 `serve`，原来「serve 尚未实现 → 返回 1」的断言随之失效；
    这条改为守住同一条精神：**坏输入必须明确失败**。
    """
    assert main(["serve", "--port", "-1"]) == 1
    err = capsys.readouterr().err
    assert "PORT_INVALID" in err


def test_serve_rejects_non_loopback_host(capsys: pytest.CaptureFixture[str]) -> None:
    """绑定面是硬约束（05-security §1.1）：`0.0.0.0` 必须被 CLI 拦下，而不是照单绑定。"""
    assert main(["serve", "--host", "0.0.0.0"]) == 2
    assert "HOST_NOT_ALLOWED" in capsys.readouterr().err


@pytest.mark.slow
def test_slow_marker_is_registered() -> None:
    """`--strict-markers` 下未注册的 marker 会报错，此用例证明注册生效。"""
    assert True
