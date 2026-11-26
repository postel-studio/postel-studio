"""S2 集成测试的公共脚手架：起真内核 / 取页面 / 抠 ticket / 连网关。

**为什么单独成文件**：`tests/test_gateway.py`（子进程黑盒）与
`tests/test_gateway_inprocess.py`（进程内 uvicorn，为了断言 `active_connection_count`）
需要同一套「从 HTML 里抠 meta」的逻辑。它不是测试文件（不带 `test_` 前缀，pytest 不收集），
也不碰 `conftest.py`——那是 S1 的回归资产，S2 不许改。

这里**只做真实 IO**：真实子进程、真实 HTTP、真实 WebSocket。没有任何 mock。
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import Final

import httpx

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
# 与 S1 一致：用用户真正的控制台入口，而不是 `python -m`（更贴近用户路径）
CLI_BIN: Final = Path(sys.executable).with_name("postel-studio")
SERVER_NAME: Final = "server.json"
BOOT_TIMEOUT: Final = 30.0
EXIT_TIMEOUT: Final = 15.0

META_TICKET: Final = re.compile(r'<meta name="postel-handshake" content="([^"]+)"')
META_WS_PATH: Final = re.compile(r'<meta name="postel-ws-path" content="([^"]+)"')
# 「页面里不许出现的东西」的判定用：状态灯元素、三个状态字面量、重连按钮、stale 规则
STATUS_STATES: Final = ("已连接", "已断开", "重连中")


class Kernel:
    """一个真实内核子进程（`postel-studio serve --port 0`）。

    用法：`with Kernel(home) as k: k.get("/") / k.ws_url(ticket) / k.stop()`。
    退出时一定保证进程被收掉，避免测试留下残留进程（S1 的同一条纪律）。
    """

    def __init__(self, home: Path) -> None:
        assert CLI_BIN.exists(), f"找不到 CLI 入口：{CLI_BIN}"
        env = dict(os.environ)
        env["POSTEL_HOME"] = str(home)
        env["HOME"] = str(home)  # 双保险：环境变量没生效也落不到真实 ~/.postel
        env.pop("PYTHONPATH", None)
        self.proc = subprocess.Popen(
            [str(CLI_BIN), "serve", "--port", "0"],
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.info = self._wait_for_server(home, self.proc.pid)
        self.port = int(str(self.info["port"]))
        self.host = str(self.info["host"])
        self.ws_path = str(self.info["wsPath"])

    @staticmethod
    def _wait_for_server(home: Path, pid: int) -> dict[str, object]:
        path = home / SERVER_NAME
        deadline = time.monotonic() + BOOT_TIMEOUT
        while time.monotonic() < deadline:
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.05)
                continue
            if isinstance(info, dict) and info.get("pid") == pid:
                return info
            time.sleep(0.05)
        raise AssertionError(f"{BOOT_TIMEOUT}s 内没等到属于 pid={pid} 的 server.json")

    # -- HTTP ---------------------------------------------------------------
    def http(self) -> httpx.Client:
        return httpx.Client(base_url=f"http://{self.host}:{self.port}", timeout=5.0)

    def get(self, path: str = "/") -> httpx.Response:
        with self.http() as client:
            return client.get(path)

    # -- WS -----------------------------------------------------------------
    def ws_url(self, ticket: str, *, path: str | None = None) -> str:
        return f"ws://{self.host}:{self.port}{path or self.ws_path}?ticket={ticket}"

    def fetch_page(self) -> tuple[str, str]:
        """取 `/`，返回 `(ticket, ws_path)`。两样都从 HTML 里正则抠出来（不引 HTML 解析库）。"""
        resp = self.get("/")
        assert resp.status_code == 200, resp.status_code
        return parse_page(resp.text)

    # -- 生命周期 ------------------------------------------------------------
    def stop(self, sig: int = signal.SIGTERM) -> int:
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
        try:
            self.proc.wait(timeout=EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:  # pragma: no cover - 只在实现回归时触发
            self.proc.kill()
            self.proc.wait(timeout=EXIT_TIMEOUT)
            raise AssertionError("内核收到信号后没有在超时内退出") from None
        return self.proc.returncode

    def kill(self) -> None:
        """SIGKILL：模拟「内核被杀」，不给任何收场机会（对应 A2 的用户路径）。"""
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=EXIT_TIMEOUT)

    def __enter__(self) -> Kernel:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.proc.poll() is None:
            self.stop()


def parse_page(page: str) -> tuple[str, str]:
    """从页面 HTML 里取出 `(ticket, ws_path)`。缺任何一个都直接失败（不返回空串蒙混）。"""
    ticket = META_TICKET.search(page)
    ws_path = META_WS_PATH.search(page)
    assert ticket is not None, "页面里没有 postel-handshake meta"
    assert ws_path is not None, "页面里没有 postel-ws-path meta"
    return ticket.group(1), ws_path.group(1)


def decode_frame(raw: str | bytes) -> dict[str, object]:
    """把一帧 JSON 解成 `dict[str, object]`（测试里到处需要，`json.loads` 返回的是 `Any`）。

    不是帧形状就直接失败：测试里拿到一个 `list` 还继续往下走，会在很远的地方炸。
    """
    parsed = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    assert isinstance(parsed, dict), f"帧不是 JSON 对象：{parsed!r}"
    return parsed


def frame_result(frame: dict[str, object]) -> dict[str, object]:
    """取 `result`（02b §2.2 的成功包络）。失败帧用它就直接失败——这本身就是断言。"""
    result = frame["result"]
    assert isinstance(result, dict), result
    return result


def frame_error_code(frame: dict[str, object]) -> str:
    """取错误码。断言用**码**，不用文案（07 §1.3）。"""
    error = frame["error"]
    assert isinstance(error, dict), error
    code = error["code"]
    assert isinstance(code, str), code
    return code


def frame_seq(frame: dict[str, object]) -> int:
    """取 `result.seq`（D4 的连接级计数器）。"""
    seq = frame_result(frame)["seq"]
    assert isinstance(seq, int) and not isinstance(seq, bool), seq
    return seq


# ---------------------------------------------------------------------------
# 进程内内核（S3 加）：真 uvicorn + 真端口 + 真 WebSocket，但在**当前进程**里跑
#
# 为什么需要：`Kernel`（上面的子进程版）用的是 `.venv/bin/postel-studio`，装载的是**已安装**
# 的包、并且 `env.pop("PYTHONPATH")`，所以任何"改源码再跑测试"的注入都改不到它。
# `tests/test_reverse_checks.py` 要验证"改 gateway 后某条测试会红"，就需要一个在**被改过的
# 源码**下运行、但又真的走网络的内核。做法抄自 `test_gateway_inprocess.py:InProcessKernel`。
# ---------------------------------------------------------------------------


class InProcessKernel:
    """后台线程里跑一个真 uvicorn（真 socket、真端口），`with` 退出时收摊。"""

    def __init__(self, ws_path: str) -> None:
        import socket
        import threading
        import time

        import uvicorn

        from postel_studio.api.app import create_app
        from postel_studio.api.server import log_config

        # socket 的所有权交给 uvicorn（它负责关）；不能用 closing()，否则会在它拿走之前关掉
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(2048)
        self.host = "127.0.0.1"
        self.port = int(sock.getsockname()[1])
        self.ws_path = ws_path
        config = uvicorn.Config(
            create_app(ws_path, host=self.host, port=self.port),
            log_config=log_config(),
            log_level="warning",
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=5,
        )
        self.server = uvicorn.Server(config)
        self._thread = threading.Thread(target=lambda: self.server.run(sockets=[sock]), daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if getattr(self.server, "started", False):
                break
            time.sleep(0.02)
        else:  # pragma: no cover - 端口/uvicorn 出问题才会走到
            raise AssertionError("进程内内核 10s 内没起来")

    def http(self) -> httpx.Client:
        return httpx.Client(base_url=f"http://{self.host}:{self.port}", timeout=5.0)

    def fetch_page(self) -> tuple[str, str]:
        """取 `/`，抠出 `(ticket, ws_path)`（与 `Kernel.fetch_page` 同义）。"""
        with self.http() as client:
            response = client.get("/")
        assert response.status_code == 200, response.status_code
        return parse_page(response.text)

    def ws_url(self, ticket: str, *, path: str | None = None) -> str:
        return f"ws://{self.host}:{self.port}{path or self.ws_path}?ticket={ticket}"

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=EXIT_TIMEOUT)
        assert not self._thread.is_alive(), "进程内内核没在超时内退出"

    def __enter__(self) -> InProcessKernel:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()


def in_process_kernel(ws_path: str) -> InProcessKernel:
    """`with in_process_kernel("/gateway/x") as k: ...`（与 `Kernel(home)` 的用法对齐）。"""
    return InProcessKernel(ws_path)
