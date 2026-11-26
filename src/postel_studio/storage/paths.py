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
import re
import sys
import tempfile
from pathlib import Path
from typing import Final

from postel_studio.domain.models import bot_name_ok

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


# ---------------------------------------------------------------------------
# Bot 的记忆空间（S6 D3）：<状态根>/bots/<repo_id>/<name>/{charter.md, memory/, sessions/}
#
# 权限是安全约束（05 §4.1）：目录 0700、文件 0600。**必须显式 chmod**——umask 022 下
# `mkdir(mode=0o700)` 只有叶子吃到 mode、父级是 0755，`write_text` 建出来是 0644（T38）。
# 所以：每层建完显式收紧，写 charter 用 `atomic_write`（mkstemp 0600），不用 write_text。
# ---------------------------------------------------------------------------

BOTS_DIR: Final = "bots"
CHARTER_FILE: Final = "charter.md"
MEMORY_DIR: Final = "memory"
SESSIONS_DIR: Final = "sessions"
# D4 的骨架：**空白待填**，不预设任何业务职责（禁止事项）。末尾一个换行。
CHARTER_TEMPLATE: Final = """# {name}

## 身份

## 职责

## 边界

## 工作方式
"""


def bots_root() -> Path:
    """`<状态根>/bots`（**不创建**）：Bot 的记忆只在状态根下，绝不写进用户仓库。"""
    return state_root() / BOTS_DIR


def bot_memory_dir(repo_id: str, name: str) -> Path:
    """`<状态根>/bots/<repo_id>/<name>`（**不创建**）。

    名字进路径前必须过白名单；再断言 `resolve()` 之后仍在 `bots_root()` 之下（D3 的纵深
    防御：白名单已足够，但将来有人放宽白名单时这一条还拦得住路径穿越）。
    """
    if not bot_name_ok(name):
        raise ValueError(f"bot 名字不合法：{name!r}")
    path = bots_root() / repo_id / name
    if not path.resolve().is_relative_to(bots_root()):
        raise ValueError(f"bot 记忆路径越出 bots 根：{path}")
    return path


def ensure_memory_space(repo_id: str, name: str) -> Path:
    """**缺什么建什么**，返回记忆目录；`charter.md` 已存在就**只读不写**（幂等，D2/D3）。"""
    target = bot_memory_dir(repo_id, name)
    for path in (
        ensure_state_root(),
        bots_root(),
        target.parent,
        target,
        target / MEMORY_DIR,
        target / SESSIONS_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
        _chmod(path, DIR_MODE)  # 显式收紧：umask 让 mkdir 的默认 mode 变松（T38）
    charter = target / CHARTER_FILE
    if not charter.exists():  # D3：已存在就不动，绝不覆盖用户编辑
        atomic_write(charter, CHARTER_TEMPLATE.format(name=name).encode("utf-8"))
    return target


def read_charter(repo_id: str, name: str) -> str:
    """读 charter 正文（UTF-8）。文件不存在时抛 `FileNotFoundError`，由调用方决定语义。"""
    return (bot_memory_dir(repo_id, name) / CHARTER_FILE).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 工作区（S7 D2）：<状态根>/worktrees/<repo_id>/<bot_name>/
#
# 事实源仍在状态根下（**绝不**在用户仓库的工作目录里建 worktree）。两层名字都进路径，
# 所以两层都要过形状检查：bot 名是用户可控的那一个（白名单，S6 D3 的同一把锁）；
# `repo_id` 来自库、理论上恒为 12 位 hex，但"理论上"不是判据——写进路径前再断言一次。
# ---------------------------------------------------------------------------

WORKTREES_DIR: Final = "worktrees"
# 06 §2.1：`repo_id` 是 12 位小写十六进制。v1 时代的老行也是（URL 哈希取前 12 位）。
_REPO_ID_RE: Final = re.compile(r"^[0-9a-f]{12}$")


def worktrees_root() -> Path:
    """`<状态根>/worktrees`（**不创建**）：所有工作区目录的唯一父级。"""
    return state_root() / WORKTREES_DIR


def worktree_dir(repo_id: str, name: str) -> Path:
    """`<状态根>/worktrees/<repo_id>/<name>`（**不创建**，也不检查它是否存在）。

    与 `bot_memory_dir` 同一条纪律：名字进路径前过白名单，再断言 `resolve()` 之后仍在
    `worktrees_root()` 之下（纵深防御：白名单已够，将来有人放宽时这一条还拦得住）。
    """
    if not bot_name_ok(name):
        raise ValueError(f"bot 名字不合法：{name!r}")
    if not _REPO_ID_RE.fullmatch(repo_id):
        raise ValueError("repo_id 必须是 12 位小写十六进制")
    path = worktrees_root() / repo_id / name
    if not path.resolve().is_relative_to(worktrees_root()):
        raise ValueError(f"工作区路径越出 worktrees 根：{path}")
    return path


def ensure_worktree_parent(repo_id: str, name: str) -> Path:
    """建好**父级**（`worktrees/` 与 `worktrees/<repo_id>/`）并收紧到 `0700`，返回目标路径。

    **不建目标目录本身**：它由 `git worktree add` 建（S7 D7——空目录 git 也接受，但让创建者
    只有 git 一个，失败时就不必判断"这个空目录是谁留下的"）。
    显式 `chmod` 的理由与 S6 相同（T38）：umask 022 下 `mkdir` 的父级是 0755。
    """
    target = worktree_dir(repo_id, name)
    for path in (ensure_state_root(), worktrees_root(), target.parent):
        path.mkdir(parents=True, exist_ok=True)
        _chmod(path, DIR_MODE)
    return target
