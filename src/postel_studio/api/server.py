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
import copy
import hashlib
import logging
import os
import re
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
from postel_studio.api.gateway import GATEWAY_PATH
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


class _WsQueryRedactor(logging.Filter):
    """把日志行里 WebSocket URL 的查询串打掉（05 §4.1：凭据绝不进日志）。

    uvicorn 的三条 WS 日志（`[accepted]` / `403` / 状态码）都打 `get_path_with_query_string()`，
    于是 `?ticket=<一次性随机串>` 会整串进内核输出——**`access_log=False` 管不着它**，
    因为它走的是 `uvicorn.error` 这个 logger，不是 `uvicorn.access`。
    ticket 是一次性的，但「一次性随机串已泄漏」仍然是泄漏：拿到它就能抢先换走这条连接。
    所以这里不关日志（HTTP 的访问行要留），只把 `?…` 之后的部分替换成 `?<REDACTED>`。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "?" in message and "WebSocket" in message:
            record.msg = re.sub(r"\?\S*", "?<REDACTED>", message)
            record.args = ()
        return True


def log_config() -> dict[str, object]:
    """uvicorn 缺省 logging 配置 + 两处改动（理由见 `_run` 与 `_WsQueryRedactor`）：

    1. 把内核自己的 `postel_studio` 挂到 stream handler 上（uvicorn 缺省配置没有 root logger，
       不挂的话我们所有 logger 都是静默的）；
    2. 给 `uvicorn.error` 挂一个脱敏过滤器（WS 日志会带 ticket）。

    只影响这两支，`propagate=False` 防止与 root 重复输出。
    """
    config: dict[str, object] = copy.deepcopy(dict(uvicorn.config.LOGGING_CONFIG))
    loggers = config.get("loggers")
    assert isinstance(loggers, dict)
    loggers["postel_studio"] = {"handlers": ["default"], "level": "INFO", "propagate": False}
    return config


def _install_ws_log_redaction() -> None:
    """给 `uvicorn.error` 挂脱敏过滤器（`log_config()` 里挂不了，见下）。

    为什么不在 dictConfig 里写：**logger 的 `filters` 列表不接受 `{"()": ...}`**（只有 handler
    的 `filters` 接受自定义工厂），写进去会抛 `ValueError: Unable to add filter {…}` 让内核
    直接起不来（实测）。`ext://` 只认模块属性，也不认类里的调用。所以只能在 `dictConfig`
    执行完之后，直接对 logger 对象 `addFilter`——它幂等（`addFilter` 不会重复加同一个对象），
    重复调用无副作用。
    """
    logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, _WsQueryRedactor) for f in logger.filters):
        logger.addFilter(_WsQueryRedactor())


def _configured_log_config() -> dict[str, object]:
    """`log_config()`，并保证脱敏过滤器已挂上（理由见 `_install_ws_log_redaction`）。"""
    _install_ws_log_redaction()
    return log_config()


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


def gateway_path(root: Path) -> str:
    """网关路径：`/gateway/<状态根的 sha256 前 16 个 hex>`（D7）。

    **稳**比**随机**重要：它决定页面与客户端拼 URL 时能不能跨重启复用。它的输入是状态根
    （同一台机器同一用户 → 同一值），不是随机数，也不是凭据——真正的凭据是页面里的 ticket。
    早先随机生成的问题实测过：重启后路径变了，旧页面拿着旧路径连不上，而浏览器无法读
    `server.json` 去发现新路径。
    """
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    return f"{GATEWAY_PATH}/{digest[:16]}"


def _run(root: Path, sock: socket.socket) -> int:
    host, port = sock.getsockname()[:2]
    _install_signal_handlers()
    _BUILDING.set()
    token = secrets.token_hex(32)
    token_file = write_token(token)
    ws_path = gateway_path(root)
    write_server_info(
        {
            "host": host,
            "port": port,
            "pid": os.getpid(),
            "apiVersion": API_VERSION,
            "version": __version__,
            "stateRoot": str(root),
            "wsPath": ws_path,
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
        create_app(ws_path, host=host, port=port),
        # uvicorn 的缺省 logging 配置里**没有 root logger 项**：不挂流处理器，我们自己模块
        # 的 logger 会一路静默（INFO/WARNING 都不会出现在 stderr）。所以把内核的 logger
        # 显式接到 uvicorn 的 `default` 流处理器上，级别随 `log_level` 一起定。
        log_config=_configured_log_config(),
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
