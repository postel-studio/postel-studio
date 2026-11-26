"""状态根与 `server.json` 的落盘约定。

这里只做文件 IO，不判断进程是否存活（那是 api.server 的职责），也不决定信号怎么处理。

- 状态根：`$POSTEL_HOME`，缺省 `~/.postel`（02-architecture §7）；环境变量为空串按缺省处理。
- 目录 `0700`、文件 `0600`（05-security §4.1）。
- 所有写入都是「同目录临时文件 + rename」：读到的永远是完整文件，不是半截。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Final

ENV_HOME: Final = "POSTEL_HOME"
SERVER_FILE: Final = "server.json"
LOCK_FILE: Final = "kernel.lock"
# S3 起内核持有用户数据（绑定关系）。**只**放在状态根里，绝不写进用户仓库；
# 文件名的事实源在这里（db.py 只是消费者），免得两处各拼一次路径。
STATE_DB_FILE: Final = "state.db"

DIR_MODE: Final = 0o700
FILE_MODE: Final = 0o600


def _chmod(path: Path, mode: int) -> None:
    """只对 POSIX 收紧权限；Windows 上 chmod 语义不同，跳过。"""
    if os.name == "posix":
        path.chmod(mode)


def state_root() -> Path:
    """返回状态根（不创建）。"""
    override = os.environ.get(ENV_HOME, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".postel").resolve()


def ensure_state_root() -> Path:
    """创建状态根并收紧到 `0700`（已存在时也收紧），返回其路径。"""
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    _chmod(root, DIR_MODE)
    return root


def server_file() -> Path:
    """`server.json`：客户端据此发现端口（02b §2.1）。"""
    return state_root() / SERVER_FILE


def state_db_file() -> Path:
    """`state.db`：仓库绑定关系等内核状态（S3 起，06-data-model §1）。**不创建**。"""
    return state_root() / STATE_DB_FILE


def lock_file() -> Path:
    """内核锁：只放决策需要的 pid / version，不放任何凭据。"""
    return state_root() / LOCK_FILE


def token_path() -> Path:
    """连接令牌文件。**不**写进 `server.json`（理由见写 server.json 的那段注释）。"""
    return state_root() / "token"


def atomic_write(path: Path, data: bytes) -> None:
    """同目录临时文件 + `Path.replace`，避免对方读到半截内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as fh:
            # mkstemp 建的是 0600，但仍显式收一次：临时文件绝不能更宽松
            _chmod(tmp_path, FILE_MODE)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def write_server_info(info: dict[str, object]) -> None:
    """写 `server.json`（`0600`）。"""
    payload = json.dumps(info, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write(server_file(), payload.encode("utf-8"))


def read_server_info() -> dict[str, object]:
    """读 `server.json`；不存在或不是 JSON 对象时返回空字典（坏文件按「没有」处理）。"""
    try:
        data = json.loads(server_file().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def remove_server_info() -> None:
    """删除 `server.json`（退出清理用）。"""
    server_file().unlink(missing_ok=True)


def write_token(token: str) -> Path:
    """原子写连接令牌（`0600`），返回路径。令牌随进程退出删除。"""
    path = token_path()
    atomic_write(path, token.encode("utf-8"))
    return path


def remove_token(path: Path) -> None:
    """删除令牌文件（退出清理用；令牌本就随进程失效）。"""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def fingerprint(token: str) -> str:
    """令牌指纹：只用于「这份 server.json 是不是我写的」，不可反推令牌。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 内核锁（单实例判定的全部 IO 都在这里，api.server 只负责流程编排）
# ---------------------------------------------------------------------------

ERR_ALREADY_RUNNING: Final = "KERNEL_ALREADY_RUNNING"


class KernelBootError(Exception):
    """拒绝启动。`code` 是稳定错误码（07-api-contract §1.3：文案可变，码不变）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def pid_alive(pid: int) -> bool:
    """`kill(pid, 0)` 探测存活；`pid<=0` 一律视为不可信 / 已死（防 `-1` 扩成进程组）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_lock_pid(path: Path) -> int | None:
    """读锁里的 pid。坏锁（非 JSON / 无 pid）返回 `None`，按「陈旧」处理。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    pid = data.get("pid") if isinstance(data, dict) else None
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None


def _lock_payload() -> str:
    """锁内容：只放决策需要的字段，不放任何凭据。版本号取自唯一的来源（包根 `__version__`）。"""
    # 函数体内导入：版本号只允许有一份（pyproject 之外唯一的事实源），这里不做模块级依赖
    from postel_studio import __version__

    return json.dumps({"pid": os.getpid(), "version": __version__}) + "\n"


def acquire_lock(path: Path, *, force: bool = False) -> None:
    """抢内核锁。pid 存活则抛 `KERNEL_ALREADY_RUNNING`；陈旧锁（或 `force`）原子覆盖。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pid = read_lock_pid(path)
        if pid is not None and pid_alive(pid):
            if not force:
                raise KernelBootError(
                    ERR_ALREADY_RUNNING,
                    f"内核已在运行（pid={pid}，见 {path}）；如需强制接管请加 --force",
                ) from None
            print(f"警告：强制接管存活内核 pid={pid} 的锁（旧进程会写不出状态）", file=sys.stderr)
        # 陈旧锁 / 强制接管：原子覆盖（写临时文件再 rename，别人不会读到半截锁）
        atomic_write(path, _lock_payload().encode("utf-8"))
        return
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(_lock_payload())
    if os.name == "posix":
        path.chmod(0o600)


def release_lock(path: Path) -> None:
    """只删自己的锁（`--force` 接管存活内核时不能把对方的锁删掉）。"""
    if read_lock_pid(path) == os.getpid():
        path.unlink(missing_ok=True)
