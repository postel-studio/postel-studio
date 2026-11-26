"""S1 · 内核存活即可见的验收测试。

约定：**每个用例都用临时 `POSTEL_HOME`**（`tests/conftest.py` 兜底 + 用例内显式传入），
绝不让测试碰到真实的 `~/.postel`。集成用例起**真实子进程**并打**真实 HTTP**，不用 mock。
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from postel_studio import __version__
from postel_studio.api.app import API_VERSION, health
from postel_studio.api.server import listen
from postel_studio.storage.paths import ERR_ALREADY_RUNNING, acquire_lock, pid_alive

REPO_ROOT = Path(__file__).resolve().parents[1]
# 用与用户相同的控制台入口（`postel-studio`），而不是 `python -m`——更贴近用户路径。
# 入口脚本与 `sys.executable` 在同一个 bin 目录，所以用它的兄弟路径而不是裸命令名（不靠 PATH）。
CLI_BIN = Path(sys.executable).with_name("postel-studio")
LOCK_NAME = "kernel.lock"
SERVER_NAME = "server.json"
BOOT_TIMEOUT = 30.0
EXIT_TIMEOUT = 15.0


def _serve_env(home: Path) -> dict[str, str]:
    """把 `HOME` 一起指到临时目录：万一环境变量没生效，也不会落到真实 `~/.postel`。"""
    env = dict(os.environ)
    env["POSTEL_HOME"] = str(home)
    env["HOME"] = str(home)
    env.pop("PYTHONPATH", None)
    return env


def _spawn(home: Path, *args: str) -> subprocess.Popen[str]:
    """起真实 CLI 子进程。参数是字面量列表，不经 shell。"""
    assert CLI_BIN.exists(), f"找不到 CLI 入口：{CLI_BIN}"
    return subprocess.Popen(
        [str(CLI_BIN), *args],
        cwd=REPO_ROOT,
        env=_serve_env(home),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stop(proc: subprocess.Popen[str], sig: int = signal.SIGTERM) -> int:
    """发信号并等进程自己退出；不退出就强杀，避免测试留下残留进程。"""
    if proc.poll() is None:
        proc.send_signal(sig)
    try:
        proc.wait(timeout=EXIT_TIMEOUT)
    except subprocess.TimeoutExpired:  # pragma: no cover - 只在实现回归时触发
        proc.kill()
        proc.wait(timeout=EXIT_TIMEOUT)
        raise AssertionError("内核收到信号后没有在超时内退出（可能有残留进程）") from None
    return proc.returncode


def _read_json(path: Path) -> dict[str, object]:
    """读一个 JSON 对象；内容不是对象时直接失败（比让它当 Any 往下流更早暴露）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path} 不是 JSON 对象"
    return data


def _read_server_info(home: Path) -> dict[str, object]:
    """server.json 必须已经存在（配合 kernel 夹具使用）。"""
    path = home / SERVER_NAME
    assert path.exists(), f"server.json 没有出现：{path}"
    return _read_json(path)


def _wait_for_server(home: Path, expect_pid: int | None = None) -> dict[str, object]:
    """等 `server.json` 出现并解析出端口。

    给了 `expect_pid` 时要求文件里就是**这个进程**：内核先清残留、再原子写新文件，
    所以「文件是我写的」才能保证端口是我的（否则会照着一个陈旧文件去连）。
    """
    path = home / SERVER_NAME
    deadline = time.monotonic() + BOOT_TIMEOUT
    while time.monotonic() < deadline:
        if path.exists():
            try:
                info = _read_json(path)
            except json.JSONDecodeError:  # pragma: no cover - 原子写不该出现半截
                time.sleep(0.05)
                continue
            if expect_pid is None or info.get("pid") == expect_pid:
                return info
        time.sleep(0.05)
    raise AssertionError(f"{BOOT_TIMEOUT}s 内没等到属于 pid={expect_pid} 的 server.json")


def _get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


@pytest.fixture
def home(state_home: Path) -> Iterator[Path]:
    """本用例专属的临时状态根。`POSTEL_HOME` 由 conftest 的 `state_home` 设置。

    测试自己是库函数的调用方（单元用例），所以这里只要路径；子进程另有 `HOME` 兜底
    （见 `_serve_env`）。绝不能用 monkeypatch 去 patch `Path.home`：它是全进程状态，
    而本模块起的是**真实子进程**，patch 只会在测试进程里生效、反而掩盖问题。
    """
    yield state_home


@pytest.fixture
def kernel(home: Path) -> Iterator[subprocess.Popen[str]]:
    """起一个真实内核，等它写 server.json，退出时确保进程被收掉。"""
    proc = _spawn(home, "serve", "--port", "0")
    try:
        _wait_for_server(home, proc.pid)
        yield proc
    finally:
        if proc.poll() is None:
            _stop(proc)


def _free_port() -> int:
    """给「端口被占」用例找一个确定的空闲端口（不是内核用的 0 号分配）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# 1. 真实子进程：黑盒验收（AC1 AC2 AC3 AC4）
# ---------------------------------------------------------------------------


def test_serve_writes_server_json_and_serves_health(kernel: subprocess.Popen[str], home: Path):
    info = _read_server_info(home)

    assert info["host"] == "127.0.0.1"
    assert isinstance(info["port"], int)
    assert info["port"] > 0
    assert info["pid"] == kernel.pid
    assert info["apiVersion"] == API_VERSION
    assert info["version"] == __version__

    # 端口确实是内核分给我们的：进程存活且能应答
    assert kernel.poll() is None
    status, body = _get(f"http://{info['host']}:{info['port']}/api/health")
    assert status == 200
    payload = json.loads(body)
    assert payload["status"] == "ok"
    assert payload["version"] == __version__
    assert payload["apiVersion"] == API_VERSION
    assert payload["pid"] == kernel.pid
    assert payload["stateRoot"] == str(home.resolve())

    # AC4：正常停止后 server.json 被删
    assert _stop(kernel) == 0
    assert not (home / SERVER_NAME).exists()


def test_page_shows_version_and_state_root(kernel: subprocess.Popen[str], home: Path):
    info = _read_server_info(home)
    _, body = _get(f"http://{info['host']}:{info['port']}/")

    assert "内核存活" in body
    assert __version__ in body
    assert str(home.resolve()) in body


def test_server_json_does_not_carry_secrets_or_token(kernel: subprocess.Popen[str], home: Path):
    """05-security §4.1：`server.json` 落盘内容不得是可直接用的凭据。"""
    raw = (home / SERVER_NAME).read_text(encoding="utf-8")
    assert "token" not in raw.lower().replace("tokenfingerprint", "")
    assert len(home.joinpath("token").read_text(encoding="utf-8")) >= 64
    _stop(kernel)


def test_sigterm_deletes_server_json_and_leaves_no_process(
    kernel: subprocess.Popen[str], home: Path
):
    info = _read_server_info(home)
    assert (home / SERVER_NAME).exists()

    assert _stop(kernel, signal.SIGTERM) == 0
    assert not (home / SERVER_NAME).exists()
    assert not (home / LOCK_NAME).exists()
    assert not pid_alive(int(str(info["pid"])))


def test_sigint_deletes_server_json_and_leaves_no_process(
    kernel: subprocess.Popen[str], home: Path
):
    """AC4 的 Ctrl+C 路径。"""
    _read_server_info(home)
    assert _stop(kernel, signal.SIGINT) == 0
    assert not (home / SERVER_NAME).exists()
    assert not (home / LOCK_NAME).exists()


def test_stdout_prints_clickable_loopback_url(kernel: subprocess.Popen[str], home: Path):
    info = _read_server_info(home)
    assert kernel.stdout is not None
    # 先停掉内核（stdout 是管道，readline 只有拿到全部输出才不会阻塞）
    _stop(kernel)
    lines = kernel.stdout.read().splitlines()
    url = f"http://127.0.0.1:{info['port']}"
    assert any(line.strip().endswith(url) for line in lines), lines
    assert all("0.0.0.0" not in line for line in lines)


def test_server_json_mode_is_600_and_state_root_is_700(kernel: subprocess.Popen[str], home: Path):
    info_path = home / SERVER_NAME
    lock_path = home / LOCK_NAME
    assert os.stat(info_path).st_mode & 0o777 == 0o600
    assert os.stat(lock_path).st_mode & 0o777 == 0o600
    assert os.stat(home).st_mode & 0o777 == 0o700
    assert os.stat(home / "token").st_mode & 0o777 == 0o600
    _stop(kernel)


# ---------------------------------------------------------------------------
# 2. AC5：锁与存活判定
# ---------------------------------------------------------------------------


def test_refuses_to_start_when_lock_pid_is_alive(home: Path):
    home.mkdir(parents=True, exist_ok=True)
    # 当前测试进程自己，必然存活
    (home / LOCK_NAME).write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")

    proc = _spawn(home, "serve", "--port", "0")
    try:
        stdout, stderr = proc.communicate(timeout=EXIT_TIMEOUT)
    finally:
        if proc.poll() is None:  # pragma: no cover
            proc.kill()

    assert proc.returncode != 0
    assert ERR_ALREADY_RUNNING in stderr
    assert str(os.getpid()) in stderr
    # 拒绝启动时不得改动已有的锁，也不得写出 server.json
    assert json.loads((home / LOCK_NAME).read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert not (home / SERVER_NAME).exists()
    assert stdout == ""


def test_overwrites_stale_lock_when_pid_is_dead(home: Path):
    home.mkdir(parents=True, exist_ok=True)
    dead_pid = 2_147_000_000  # 大于 /proc/sys/kernel/pid_max，必然不存在
    assert not pid_alive(dead_pid)
    lock = home / LOCK_NAME
    lock.write_text(json.dumps({"pid": dead_pid}), encoding="utf-8")
    # 顺带留一个坏掉的旧 server.json，证明也能被覆盖
    (home / SERVER_NAME).write_text('{"pid": 1, "port": "oops"}', encoding="utf-8")

    proc = _spawn(home, "serve", "--port", "0")
    try:
        info = _wait_for_server(home, proc.pid)
        assert info["port"] != "oops"
        assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == proc.pid
        status, _ = _get(f"http://127.0.0.1:{info['port']}/api/health")
        assert status == 200
        assert _stop(proc) == 0
    finally:
        if proc.poll() is None:  # pragma: no cover
            proc.kill()
    assert not lock.exists()
    assert not (home / SERVER_NAME).exists()


def test_garbage_lock_is_treated_as_stale(home: Path):
    """坏锁（非 JSON）不得让内核永远起不来。"""
    home.mkdir(parents=True, exist_ok=True)
    (home / LOCK_NAME).write_text("not-json", encoding="utf-8")

    proc = _spawn(home, "serve", "--port", "0")
    try:
        _wait_for_server(home, proc.pid)
        assert _stop(proc) == 0
    finally:
        if proc.poll() is None:  # pragma: no cover
            proc.kill()


def test_executable_port_refuses_busy_port(home: Path):
    """显式端口被占时给出稳定错误码，而不是栈。"""
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = int(blocker.getsockname()[1])
    try:
        proc = _spawn(home, "serve", "--port", str(port))
        _, stderr = proc.communicate(timeout=EXIT_TIMEOUT)
        assert proc.returncode != 0
        assert "LISTEN_FAILED" in stderr
        assert not (home / LOCK_NAME).exists()
        assert not (home / SERVER_NAME).exists()
    finally:
        blocker.close()


def test_rejects_host_other_than_loopback(home: Path):
    """AC3 的硬约束：命令行不给绑 0.0.0.0 的口子。"""
    proc = _spawn(home, "serve", "--host", "0.0.0.0")
    _, stderr = proc.communicate(timeout=EXIT_TIMEOUT)
    assert proc.returncode != 0
    assert "HOST_NOT_ALLOWED" in stderr
    assert not home.exists()


# ---------------------------------------------------------------------------
# 3. 进程内小单元（真实的 socket / 文件操作，不 mock）
# ---------------------------------------------------------------------------


def test_acquire_lock_overwrites_when_forced(home: Path, capsys: pytest.CaptureFixture[str]):
    home.mkdir(parents=True, exist_ok=True)
    lock = home / LOCK_NAME
    lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")

    acquire_lock(lock, force=True)

    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert "警告" in capsys.readouterr().err


def test_listen_binds_loopback_and_accepts_kernel_assigned_port():
    sock = listen(0)
    try:
        host, port = sock.getsockname()[:2]
        assert host == "127.0.0.1"
        assert port > 0
    finally:
        sock.close()


def test_pid_alive_rejects_non_positive_values():
    """`kill(-1, 0)` 会扩成「信号发给所有进程」，必须挡在门外。"""
    assert not pid_alive(0)
    assert not pid_alive(-1)


# ---------------------------------------------------------------------------
# 4. health 内容：直接调路由函数（不起子进程的快速路径）
# ---------------------------------------------------------------------------


def test_health_payload_contains_version_and_state_root(home: Path):
    payload = health()

    assert payload["version"] == __version__
    assert payload["apiVersion"] == API_VERSION
    assert payload["stateRoot"] == str(home.resolve())
    assert payload["status"] == "ok"
