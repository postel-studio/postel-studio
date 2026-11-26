"""内核进程生命周期：抢锁 → 分端口 → 起 uvicorn（前台）→ 退出即清场。

顺序是有讲究的（客户端以 `server.json` 为「内核已经在听」的信号，02b §2.1）：

1. `O_CREAT|O_EXCL` 原子抢锁；锁在但 pid 已死（或被 `--force` 接管）→ 原子覆盖。
2. 先 `bind(0)` 让**内核**拿到空闲端口，再把同一个 socket 交给 uvicorn，避免「取端口」与
   「监听端口」之间的空窗竞态。绝不给 uvicorn 传 `port=`/`fd=`：那会 reload 子进程化，
   并关掉继承来的监听 socket。
3. **先装信号处理器，再写 `server.json`**：反过来的窗口期里一个 SIGTERM 会走缺省动作，
   `finally` 不执行，留下残留文件。
4. 信号处理器与 Ctrl+C 同路（抛 `KeyboardInterrupt`），由 uvicorn 负责优雅收摊。

退出清理：`server.json` 永远删（它描述的是「谁在听」，自己停了就不再成立）；锁文件只删
pid 属于自己的（`--force` 接管时不能删对方的锁）；令牌随进程失效。
"""

from __future__ import annotations

import contextlib
import os
import secrets
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from types import FrameType
from typing import Final

import uvicorn

from postel_studio import __version__
from postel_studio.api.app import API_VERSION, create_app
from postel_studio.storage.paths import (
    KernelBootError,
    acquire_lock,
    ensure_state_root,
    fingerprint,
    lock_file,
    release_lock,
    remove_server_info,
    remove_token,
    server_file,
    write_server_info,
    write_token,
)

LOOPBACK: Final = "127.0.0.1"
GRACE_SECONDS: Final = 5
BUILD_SETTLE_SECONDS: Final = 0.2
LISTEN_BACKLOG: Final = 2048

ERR_PORT_INVALID: Final = "PORT_INVALID"
ERR_LISTEN_FAILED: Final = "LISTEN_FAILED"

# 是否在「建设期」：从装好信号处理器起到 uvicorn 接管信号为止。这个窗口（实测约 15~20ms）
# 里收到 SIGTERM 不能抛 `KeyboardInterrupt`——那会直接跳出 `serve()`，`finally` 里的收场被
# 完全跳过，盘上留下 server.json 与锁（实测：客户端一看见锁文件就发信号的模式必然踩到）。
# 改为记账，等 uvicorn 建好再自己停；在此之前到达的信号按「不抛异常」处理，进程不会留下残骸。
_BUILDING = threading.Event()
_STOP = threading.Event()


def listen(port: int = 0) -> socket.socket:
    """分配并监听端口。**只**绑 `127.0.0.1`（05-security §1.1）；`port=0` 由内核挑空闲端口。"""
    if not 0 <= port < 65536:
        raise KernelBootError(ERR_PORT_INVALID, f"--port 必须在 0..65535 之间，收到 {port}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if port:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((LOOPBACK, port))  # port=0 → 内核分配；任何情况下都不会绑到 0.0.0.0
        sock.listen(LISTEN_BACKLOG)
    except OSError as exc:
        sock.close()
        raise KernelBootError(ERR_LISTEN_FAILED, f"端口 {port} 无法监听：{exc.strerror}") from exc
    return sock


def serve(port: int = 0, *, force: bool = False) -> int:
    """前台常驻。返回进程退出码（正常停止为 0，拒绝启动为 1）。

    本函数只应在进程主线程调用（信号处理器需要主线程）。
    """
    _STOP.clear()
    root = ensure_state_root()
    lock = lock_file()
    # 旧内核崩溃残留的 server.json 先清掉：「能看见 server.json」应等价于「内核已经在听」
    with contextlib.suppress(OSError):
        remove_server_info()
    try:
        acquire_lock(lock, force=force)
        sock = listen(port)
    except KernelBootError as exc:
        release_lock(lock)
        print(str(exc), file=sys.stderr)
        return 1
    try:
        return _run(root, sock)
    finally:
        release_lock(lock)


def _install_signal_handlers() -> None:
    """把 SIGTERM 与 SIGINT 统一成「优雅停止」，Ctrl+C 走同一条路。

    必须**早于**写 `server.json`：客户端一看见文件就可能发信号，而缺省动作不抛异常、
    直接终止进程，收场代码（删文件、删锁）根本不会执行。

    处理器抛 `KeyboardInterrupt`，与 Ctrl+C 同路：uvicorn 在事件循环里把它转成取消并优雅收摊。

    `signal.signal` 只能在主线程调用；`serve` 若不在主线程，交由缺省行为（直接终止）。
    """
    if threading.current_thread() is not threading.main_thread():
        return

    def _on_stop_signal(signum: int, frame: FrameType | None) -> None:
        _STOP.set()
        if _BUILDING.is_set():
            return  # 建设期：只记账，交给 _await_ready 之后的收场路径
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_stop_signal)
    signal.signal(signal.SIGINT, _on_stop_signal)


def _run(root: Path, sock: socket.socket) -> int:
    host, port = sock.getsockname()[:2]
    _install_signal_handlers()
    _BUILDING.set()
    token = secrets.token_hex(32)
    token_file = write_token(token)
    write_server_info(
        {
            "host": host,
            "port": port,
            "pid": os.getpid(),
            "apiVersion": API_VERSION,
            "version": __version__,
            "stateRoot": str(root),
            # 令牌本体**不进** server.json（05-security §1.1 要求写明文令牌；S1 还没有
            # WebSocket 客户端可发 Bearer，写进去只会多一处长期凭据）。这里只放指纹，
            # 供退出时判断「这份 server.json 是不是我写的」。详见交付说明的偏离项。
            "tokenFingerprint": fingerprint(token),
        }
    )

    url = f"http://{host}:{port}"
    print(f"Postel Studio 内核 v{__version__} 已启动", flush=True)
    print(f"  地址     {url}", flush=True)
    print(f"  健康检查 {url}/api/health", flush=True)
    print(f"  状态根   {root}", flush=True)
    print("Ctrl+C 停止", flush=True)

    config = uvicorn.Config(
        create_app(),
        log_level="info",
        access_log=False,
        lifespan="off",
        timeout_graceful_shutdown=GRACE_SECONDS,
    )
    server = uvicorn.Server(config)
    try:
        if _STOP.is_set():
            # 建设期收到过停止请求：等 uvicorn 建好再让它收摊（见 _BUILDING 的注释）
            time.sleep(BUILD_SETTLE_SECONDS)
            server.should_exit = True
        _BUILDING.clear()
        server.run(sockets=[sock])
    except KeyboardInterrupt:  # pragma: no cover - 个别平台的退出路径
        pass
    finally:
        sock.close()
        with contextlib.suppress(OSError):
            remove_server_info()  # 它描述的是「谁在听」，自己停了就不再成立
        remove_token(token_file)  # 令牌随进程退出失效
        print(f"内核已退出，{server_file()} 已清理", flush=True)
    return 0
