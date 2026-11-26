"""`Repo` 实体与 `repo_id` 派生：**纯函数，无 IO**（06-data-model §2.1）。

为什么单独成一模块而不是塞进 `api/repository.py`：`repo_id()` 是被断言过契约的东西
（D3：https / ssh 两种 URL 必须得到同一个 id，长度必须是 12 位），拿 `git` 或 DB 才能测它
会让这条不变量变成集成测试的副产物。这里三个函数都只吃字符串。

`repo_id` 的算法照 06 §2.1：`sha256(归一化 origin URL)[:12]`，**无 origin 时用绝对路径**。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# 06 §2.1：12 位十六进制（07 §4.1 的示例 `adec6540a725` 就是 12 位）
REPO_ID_LENGTH: Final = 12

# `user@host:path`（scp-like）与 `scheme://[user@]host[:port]/path` 两种写法。
# 前者只认 `host:path` 这一种冒号分隔（避免把 Windows 盘符 `C:\...` 当成 ssh 地址）。
_SCP_LIKE: Final = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>.+)$")
_URL_LIKE: Final = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<rest>.+)$")


@dataclass(frozen=True, slots=True)
class Repo:
    """一个已绑定的本地仓库（06 §2.1 的 Repo 实体）。

    字段名用 `snake_case`（落盘与内部统一），帧层在 `api/repository.py` 的边界上转成
    `camelCase`（07 §4.2：转换只在边界发生，内部不混用）。
    """

    repo_id: str
    path: str  # 已 resolve 的绝对路径，且**一定是仓库根**（D2）
    origin_url: str | None
    default_branch: str | None
    added_at: float

    @property
    def name(self) -> str:
        """左栏显示名：根目录名（`/home/u/proj/src` 绑定后显示 `proj`，不是 `src`）。"""
        return Path(self.path).name or self.path


def normalize_origin(url: str) -> str:
    """把 origin URL 归一化成一个**与传输方式无关**的标识（D3 / 06 §2.1 不变量）。

    | 输入 | 归一化结果 |
    |---|---|
    | `https://github.com/o/p.git` | `github.com/o/p` |
    | `git@github.com:o/p.git` | `github.com/o/p` |
    | `ssh://git@github.com:22/o/p.git` | `github.com/o/p` |
    | `git://github.com/o/p` | `github.com/o/p` |
    | `/srv/git/p.git`（本地路径 remote） | 原样（`resolve` 都做不到，别猜） |

    host 统一小写、去掉尾部 `.git` 与尾部 `/`、丢弃 scheme / user / 端口 /
    `ssh://` 默认端口。**不做** URL 解码：解码会让两种来源的差异被放大，而不是被抹平。
    """
    text = url.strip()
    if not text:
        return ""
    match = _URL_LIKE.match(text)
    if match is not None:
        rest = match.group("rest")
        # 去掉 `user@`（凭据不进哈希：D3 要的是同一个仓库，不是同一个用户名）
        rest = rest.split("@", 1)[-1]
        host, _, path = rest.partition("/")
        host = host.split(":", 1)[0].lower()  # 端口不参与：22 与 443 是同一个仓库
    else:
        scp = _SCP_LIKE.match(text)
        if scp is not None and not text.startswith("/"):
            host = scp.group("host").lower()
            path = scp.group("path")
        else:
            return text.rstrip("/")
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return f"{host}/{path}" if path else host


def repo_id_for_origin(origin_url: str) -> str:
    """`sha256(归一化 origin URL)` 的前 12 个 hex（06 §2.1）。"""
    digest = hashlib.sha256(normalize_origin(origin_url).encode("utf-8")).hexdigest()
    return digest[:REPO_ID_LENGTH]


def repo_id_for_path(path: str) -> str:
    """无 origin 时的让步（D3）：`sha256(绝对路径)` 的前 12 个 hex。

    **已知不完美**：仓库后来加了 origin，`repo_id` 会从路径哈希变成 URL 哈希——
    同一台机器上同一个仓库看起来"变了一个"。本刀不做迁移，见 codemap 未决问题。
    """
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
    return digest[:REPO_ID_LENGTH]


def repo_id(path: str, origin_url: str | None) -> str:
    """有 origin 用 URL，没有用绝对路径（D3）。"""
    if origin_url:
        return repo_id_for_origin(origin_url)
    return repo_id_for_path(path)
