"""`Repo` 实体与 id 派生：**纯函数，无 IO**（06-data-model §2.1）。

为什么单独成一模块而不是塞进 `api/repository.py`：这里每个函数都是**被断言过契约**的东西
（S3.5 D1：`repo_id` 只由路径决定、长度 12；`origin_hash` 对 https / ssh 两种写法相同），
拿 `git` 或 DB 才能测它们会让这些不变量变成集成测试的副产物。这里所有函数都只吃字符串。

`repo_id` 的算法照 06 §2.1 + S3.5 **D1**：`sha256(绑定时 resolve 后的绝对路径)[:12]`，
**与 origin 无关**——origin 的哈希降级成 `origin_hash`（D2），只用于识别"同一个远端仓库"。
S3 的算法是"有 origin 用 URL 哈希、没有才用路径"，后果是用户加一个 remote 仓库身份就变了
（`docs/design/08-repo-id-stability.md` §1 的实测）。
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
    # S3.5 D2：归一化 origin 的哈希，**只用于识别"是不是同一个远端仓库"**，不参与主键/外键。
    # 默认 `None` 是为了让不关心它的调用方（测试、将来的重建路径）不必编一个值。
    origin_hash: str | None = None

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
    """`sha256(归一化 origin URL)` 的前 12 个 hex——**只给 `origin_hash` 用**（S3.5 D2）。

    名字里的 `repo_id` 是历史遗留（S3 时代它真的算 `repo_id`）；保留它是因为 S3 的测试与
    codemap 都按这个名字索引。**不要**再用它给 `Repo.repo_id` 赋值：那会退回 D1 之前的缺陷。
    """
    digest = hashlib.sha256(normalize_origin(origin_url).encode("utf-8")).hexdigest()
    return digest[:REPO_ID_LENGTH]


def repo_id_for_path(path: str) -> str:
    """`repo_id` 的唯一算法（S3.5 D1）：`sha256(绝对路径)` 的前 12 个 hex。

    **一次性计算、写后永不更新**（D1）：`RepoStore.upsert` 不会改写已存在行的 `repo_id`。
    所以加/改/删 remote、切分支、fetch 都不影响它；只有仓库**被移动到新路径**时才会变——
    那比"加 remote"罕见得多，且移动仓库本来就需要人确认（S19 的 doctor）。
    """
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
    return digest[:REPO_ID_LENGTH]


def origin_hash(origin_url: str | None) -> str | None:
    """`origin_url` → `sha256(归一化 URL)[:12]`；没有 origin 就是 `None`（S3.5 D2）。

    **不再是 `repo_id`**（S3 时代它是）：它只回答"两个绑定是不是同一个远端仓库"——将来做
    去重提示时会用到——**不参与任何主键或外键**。所以它**可变**：改 remote 它就变，这没风险，
    因为没人按它查行。`None` 输入给 `None` 而不是 `sha256("")` 的哈希：凭空创造一个"空 origin"
    的身份会让两个无 origin 的仓库看起来是同一个远端。
    """
    if not origin_url:
        return None
    return repo_id_for_origin(origin_url)


def repo_id(path: str) -> str:
    """`repo_id = sha256(绝对路径)[:12]`（S3.5 D1：**只看路径**，不看 origin）。

    参数里**没有** origin：S3 的签名是 `repo_id(path, origin_url)`，把 origin 传进来就等于
    允许"origin 变了 id 就变"。调用方（`api/repository.py:bind`）拿到 origin 后单独算
    `origin_hash`，那是另一个字段（D2）。
    """
    return repo_id_for_path(path)
