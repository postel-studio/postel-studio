"""S9 的验收：真握手打真 Agent，异常路径打假服务端，回收有 pid 证据。

分工是刻意的（规格 §3）：
- 真 `opencode acp` 只证明「这条路真的通」——没装就 skip，不假装通过；
- `fake_acp.py` 负责真 Agent 造不出来的情况：噪声行、永不应答、缺字段、答完赖着不死。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from postel_studio.agent import acp_client, registry
from postel_studio.api import frames
from postel_studio.api.agents import AgentsService
from postel_studio.web import assets

FIXTURE = Path(__file__).parent / "fixtures" / "fake_acp.py"
REAL_AGENT = shutil.which("opencode")

# §8.1 的实测响应，逐字段抄下来——这是「能力从应答来」的对照物，不是实现的一部分。
MEASURED_CAPS: dict[str, object] = {
    "loadSession": True,
    "mcpCapabilities": {"http": True, "sse": True},
    "promptCapabilities": {"embeddedContext": True, "image": True},
    "sessionCapabilities": {"close": {}, "fork": {}, "list": {}, "resume": {}},
}
MEASURED_FLAGS = (
    "loadSession",
    "mcp.http",
    "mcp.sse",
    "prompt.embeddedContext",
    "prompt.image",
    "session.close",
    "session.fork",
    "session.list",
    "session.resume",
)


def _fake(mode: str = "ok", **flags: str) -> list[str]:
    command = [sys.executable, str(FIXTURE), mode]
    command += [f"--{name}={value}" for name, value in flags.items()]
    return command


def _use_fake(monkeypatch: pytest.MonkeyPatch, mode: str = "ok", **flags: str) -> list[str]:
    command = _fake(mode, **flags)
    monkeypatch.setenv(registry.COMMAND_ENV, json.dumps(command))
    return command


def _read_pid(path: Path) -> int:
    """假服务端刚 spawn 时 pid 文件可能还没落盘：给 1 秒，别让测试自己不稳。"""
    for _ in range(100):
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return int(path.read_text(encoding="utf-8"))
        time.sleep(0.01)
    raise AssertionError(f"假服务端没有写出 pid：{path}")


def _assert_pid_gone(pid: int) -> None:
    """pid 不存在 = 被回收。zombie 会让 `os.kill(pid, 0)` 成功，所以这条也顺带证明 wait 过。"""
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# ---------------------------------------------------------------- 验收 1：真握手


@pytest.mark.skipif(REAL_AGENT is None, reason="本机没有 opencode，验收 1 只在装了它的机器上成立")
def test_real_handshake_reports_protocol_and_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """**验收 1**：版本与能力来自 `initialize` 应答，不是常量。"""
    monkeypatch.delenv(registry.COMMAND_ENV, raising=False)
    handshake = acp_client.initialize(["opencode", "acp"], timeout_s=30)
    assert handshake.protocol_version == 1
    assert handshake.agent_version, "真 Agent 必须报出版本"
    assert handshake.agent_name, "真 Agent 必须报出名字"
    assert "session.list" in handshake.flags
    assert "mcp.http" in handshake.flags


# ---------------------------------------------------------------- 验收 3：能力是算出来的


def test_flags_match_the_measured_response() -> None:
    """**验收 3**：§8.1 的响应恰好摊成九个 flag，且顺序固定（排序即契约）。"""
    assert acp_client.flatten_capabilities(MEASURED_CAPS) == MEASURED_FLAGS


@pytest.mark.parametrize(
    ("caps", "expected"),
    [
        ({}, ()),
        (None, ()),
        ("not a mapping", ()),
        ({"loadSession": True}, ("loadSession",)),
        ({"loadSession": False}, ()),
        ({"fooCapabilities": {"bar": {}}}, ("foo.bar",)),  # 空对象 = 支持
        ({"fooCapabilities": {"bar": False}}, ()),
        ({"plain": {"nested": True}}, ("plain.nested",)),
    ],
)
def test_flags_are_derived_not_hardcoded(caps: object, expected: tuple[str, ...]) -> None:
    """纯函数的边界：输入变，输出必须跟着变（反向注入 R1 打的就是这条）。"""
    assert acp_client.flatten_capabilities(caps) == expected


# ---------------------------------------------------------------- 验收 2 / 4：异常路径


def test_missing_executable_is_unavailable() -> None:
    """**验收 2** 的后端一半：命令不存在 → `ADAPTER_UNAVAILABLE`，不是崩。"""
    with pytest.raises(acp_client.AcpUnavailableError):
        acp_client.initialize(["definitely-not-a-real-agent-xyz"], timeout_s=1)


def test_a_string_command_is_refused() -> None:
    """05 §3.3：命令必须是数组。整串会被当成一个可执行名（或更糟，被 shell 解释）。

    注意 `str` **满足** `Sequence[str]`，所以 mypy 不会拦这一行——类型系统帮不上忙，
    这就是 `initialize` 里那条运行时 `isinstance(command, str)` 守卫存在的理由。
    """
    with pytest.raises(acp_client.AcpUnavailableError):
        acp_client.initialize("opencode acp", timeout_s=1)


def test_missing_executable_is_unavailable_through_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake(monkeypatch)
    monkeypatch.setenv(registry.COMMAND_ENV, json.dumps(["definitely-not-a-real-agent-xyz"]))
    with pytest.raises(frames.FrameError) as exc:
        AgentsService(timeout_s=1).capabilities({"agentKind": registry.ACP_STDIO})
    assert exc.value.code == "ADAPTER_UNAVAILABLE"


def test_non_json_lines_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """噪声行（banner / 日志 / 空行）被跳过，合法响应照样读得到。"""
    command = _use_fake(monkeypatch, "garbage")
    handshake = acp_client.initialize(command, timeout_s=10)
    assert handshake.protocol_version == 1
    assert handshake.agent_name == "Fake Agent"


def test_missing_protocol_version_is_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    """答了但不是 initialize 结果 → `INTERNAL`。说「没安装」是撒谎。"""
    _use_fake(monkeypatch, "no-version")
    with pytest.raises(frames.FrameError) as exc:
        AgentsService(timeout_s=10).capabilities({"agentKind": registry.ACP_STDIO})
    assert exc.value.code == "INTERNAL"


def test_exit_before_answering_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """D6 / `07 §4.11`：进程在应答前退出 → `ADAPTER_UNAVAILABLE`，**不是**超时。

    走 `_exchange` 的 EOF 分支（`os.read` 回 `b""`）；这条分支此前只在覆盖率报告里出现过。
    `timeout_s=5` 是刻意的：如果实现把它误判成超时，断言会拿到 `ADAPTER_TIMEOUT` 而红，
    而不是靠「反正都会抛」蒙混过去。
    """
    _use_fake(monkeypatch, "exit-early")
    with pytest.raises(frames.FrameError) as exc:
        AgentsService(timeout_s=5).capabilities({"agentKind": registry.ACP_STDIO})
    assert exc.value.code == "ADAPTER_UNAVAILABLE"


def test_no_answer_within_deadline_is_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """**验收 4**：到点没答 → `ADAPTER_TIMEOUT`（产品默认 30s，测试用 0.3s）。"""
    _use_fake(monkeypatch, "silent")
    with pytest.raises(acp_client.AcpTimeoutError):
        acp_client.initialize(_fake("silent"), timeout_s=0.3)


def test_timeout_maps_to_the_stable_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_fake(monkeypatch, "silent")
    with pytest.raises(frames.FrameError) as exc:
        AgentsService(timeout_s=0.3).capabilities({"agentKind": registry.ACP_STDIO})
    assert exc.value.code == "ADAPTER_TIMEOUT"


# ---------------------------------------------------------------- 验收 5：回收


def test_child_is_reaped_after_success(tmp_path: Path) -> None:
    """**验收 5**：正常答完也必须被杀——假服务端答完是**故意不退**的。"""
    pidfile = tmp_path / "pid"
    handshake = acp_client.initialize(_fake("ok", pidfile=str(pidfile)), timeout_s=10)
    assert handshake.protocol_version == 1
    _assert_pid_gone(_read_pid(pidfile))


def test_child_is_reaped_after_timeout(tmp_path: Path) -> None:
    """**验收 5**：超时路径也一样（反向注入 R2 打的就是这条）。"""
    pidfile = tmp_path / "pid"
    with pytest.raises(acp_client.AcpTimeoutError):
        acp_client.initialize(_fake("silent", pidfile=str(pidfile)), timeout_s=0.3)
    _assert_pid_gone(_read_pid(pidfile))


# ---------------------------------------------------------------- 安全：环境变量


def test_child_env_has_no_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`06 T22` / D8：读**子进程自己看到的**环境，不读我们的过滤器。"""
    envfile = tmp_path / "env.json"
    monkeypatch.setenv("MY_SECRET_TOKEN", "s3cr3t")
    monkeypatch.setenv("DEPLOY_KEY", "k")
    monkeypatch.setenv("SAFE_VAR_FOR_TEST", "kept")
    acp_client.initialize(_fake("ok", envfile=str(envfile)), timeout_s=10)
    seen = json.loads(envfile.read_text(encoding="utf-8"))
    for name in ("MY_SECRET_TOKEN", "DEPLOY_KEY", "SSH_AUTH_SOCK"):
        assert name not in seen, name
    assert seen["SAFE_VAR_FOR_TEST"] == "kept"


# ---------------------------------------------------------------- 通道与覆盖


def test_broken_override_is_unavailable_not_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """D2：覆盖值坏了就报不可用——**不回退默认**，否则用户看的是另一个 Agent。"""
    monkeypatch.setenv(registry.COMMAND_ENV, "{not json")
    service = AgentsService(timeout_s=1)
    with pytest.raises(frames.FrameError) as exc:
        service.capabilities({"agentKind": registry.ACP_STDIO})
    assert exc.value.code == "ADAPTER_UNAVAILABLE"
    assert service.list()[0]["available"] is False


@pytest.mark.parametrize(
    "raw",
    ["[]", "[1]", '["ok", ""]', '{"command": ["ok"]}'],
    ids=["empty-array", "non-string-element", "empty-string-element", "not-an-array"],
)
def test_broken_override_shapes_are_refused(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    """D2 的四种坏形状都 fail-closed；此前只测了非法 JSON。

    同时钉住 `agent.list` 的语义：`available=False`，而 `command` 仍是登记表默认值——
    它只用于诊断，不会被执行（`capabilities()` 会先撞上同一个异常）。
    """
    monkeypatch.setenv(registry.COMMAND_ENV, raw)
    channel = registry.resolve(registry.ACP_STDIO)
    assert channel is not None
    with pytest.raises(registry.BrokenOverrideError):
        registry.command_for(channel)
    entry = AgentsService().list()[0]
    assert entry["available"] is False
    assert entry["command"] == list(channel.command)


def test_command_for_defaults_to_the_registry_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2 第一行：未设置或只有空白，都回登记表默认（`command_for` 的默认分支）。"""
    channel = registry.resolve(registry.ACP_STDIO)
    assert channel is not None
    monkeypatch.delenv(registry.COMMAND_ENV, raising=False)
    assert registry.command_for(channel) == channel.command
    monkeypatch.setenv(registry.COMMAND_ENV, "   ")
    assert registry.command_for(channel) == channel.command


def test_agent_list_reports_availability_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D4：`agent.list` 只查文件在不在，起进程的事交给 `agent.capabilities`。"""
    service = AgentsService()
    monkeypatch.setenv(registry.COMMAND_ENV, json.dumps([sys.executable]))
    assert service.list()[0]["available"] is True
    monkeypatch.setenv(registry.COMMAND_ENV, json.dumps(["definitely-not-a-real-agent-xyz"]))
    assert service.list()[0]["available"] is False


def test_registry_has_one_channel_named_after_the_transport() -> None:
    """D1：id 是传输名，不是产品名；方法名的禁令也在这里被守住。"""
    kinds = [channel.agent_kind for channel in registry.channels()]
    assert kinds == [registry.ACP_STDIO]
    assert "opencode" not in registry.ACP_STDIO
    assert registry.resolve("opencode") is None


# ---------------------------------------------------------------- 帧契约


def test_capabilities_rejects_a_target() -> None:
    """D5：`target` 在第一版是保留字，非 null 一律 `NOT_SUPPORTED`。"""
    with pytest.raises(frames.FrameError) as exc:
        AgentsService().capabilities({"agentKind": registry.ACP_STDIO, "target": "/tmp"})
    assert exc.value.code == "NOT_SUPPORTED"


@pytest.mark.parametrize(
    "params",
    [{}, {"agentKind": ""}, {"agentKind": 1}, {"agentKind": None}, {"agentKind": "nope"}],
)
def test_bad_agent_kind_is_invalid_params(params: dict[str, object]) -> None:
    with pytest.raises(frames.FrameError) as exc:
        AgentsService().capabilities(params)
    assert exc.value.code == "INVALID_PARAMS"


# ---------------------------------------------------------------- 页面契约（D9 / D10）


def test_page_has_the_agent_card_contract() -> None:
    """D9：id 是契约；卡片在仓库面板**之前**（内核级能力，不属于某个仓库）。"""
    page = assets.INDEX_HTML
    for fragment in (
        '<section id="agent" data-state="idle"',
        '<h2 id="agent-title">Agent 通道</h2>',
        '<button id="agent-detect" type="button">检测 Agent</button>',
        '<p id="agent-error" role="alert" hidden></p>',
        'id="agent-detail" hidden',
        'id="agent-kind"',
        'id="agent-version"',
        'id="agent-protocol"',
        'id="agent-elapsed"',
        'id="agent-flags"',
    ):
        assert fragment in page, fragment
    assert page.index('id="agent"') < page.index('id="branches"')


def test_page_agent_error_text_is_not_the_git_message() -> None:
    """D10：全局表里 `ADAPTER_UNAVAILABLE` 说的是「找不到 git 命令」，Agent 卡不能复用它。"""
    page = assets.INDEX_HTML
    assert "本机没有可用的 Agent 命令" in page
    assert "Agent 握手超时" in page
    assert "找不到 git 命令" in page  # 全局表还在，别的面板还要用
