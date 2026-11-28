"""错误码 → 用户可读文案，以及 `message` 的脱敏（S18 D1/D4）。

**单一来源**：页面里的 `ERROR_TEXT` 由 `render_index(error_text=...)` 注入，`web/assets.py`
不再保留第二份（那份副本曾经跟着码表演进，漏一条就是一个面板答"绑定失败"）。

两条纪律：

1. **按码映射，不读 `message`**（07 §1.3：文案可改，码不能改）。表外的码走
   `UNKNOWN_CODE_TEXT`，绝不把服务端原文贴到页面上。
2. `message` 只给日志与调试，但它会进 devtools、用户贴的截图。`frames.encode_error` 在
   编码前统一过 `message_safe`：绝对路径、堆栈、凭据片段都不许出内核。
"""

from __future__ import annotations

import re
from typing import Final

# 07 §1.3 的**全部**帧错误码（本刀补登记 `FRAME_TOO_LARGE`，见 07 的同 PR 修订）。
# 键就是契约：测试枚举 07 表与源码里的码字面量，两处都必须被这里覆盖。
ERROR_TEXT: Final[dict[str, str]] = {
    "INVALID_PARAMS": "参数不合法，检查一下再试",
    "FRAME_TOO_LARGE": "请求太大（单帧超过 1 MiB），拆小后再试",
    "UNKNOWN_METHOD": "内核不支持这个操作（方法不存在）",
    "NOT_A_REPO": "不是 git 仓库",
    "REPO_NOT_BOUND": "仓库未绑定或已不存在",
    "BAD_REF": "分支或提交不存在",
    "BAD_BASE_REF": "基础分支不存在或不是提交",
    "BAD_CWD": "请输入绝对路径（不能是相对路径或 ~ 开头的路径）",
    "PATH_OUTSIDE_ROOT": "路径越出了允许的根目录",
    "PERMISSION_DENIED": "这个 Bot 是 L0（只读），先在 Charter 里把权限改到 L1 以上",
    "NOT_SUPPORTED": "仓库含非 UTF-8 的名称，第一版不支持",
    "WORKTREE_CONFLICT": "工作区目录被占用，或里面有未提交的改动",
    "ALREADY_EXISTS": "这个仓库已经有同名的 Bot，换个名字再试",
    "ADAPTER_UNAVAILABLE": "内核找不到 Git 或 Agent 命令",
    "ADAPTER_TIMEOUT": "Agent 握手超时，确认它能启动后再试",
    "CONTEXT_CONFLICT": "上下文规范冲突，请先解决仓库与 charter 的权限声明差异",
    "SCOPE_VIOLATION": "会话尝试越界（切分支或写越界），已阻断",
    "BUSY": "这个 Bot 已有任务在跑，等它结束再派",
    "INTERNAL": "内核处理失败，请查看启动它的终端",
}

# 表外的码（不该出现；出现了也别把码/服务端原文当成人话给用户）。
UNKNOWN_CODE_TEXT: Final = "操作失败：内核返回了未登记的错误码，请把启动它的终端输出报给维护者"

# 07 §1.3 表里的码 + 本文件表外的兜底。测试用它对账，不靠人肉维护第二份清单。
DOCUMENTED_CODES: Final[frozenset[str]] = frozenset(ERROR_TEXT)

# -- message 脱敏 ------------------------------------------------------------
# 顺序有意义：先 `~/`，再 Windows 盘符，最后 POSIX 绝对路径（否则 `~/x` 会被当成 `/x`）。
_HOME_PATH: Final = re.compile(r"~/[^\s\"'<>]*")
_WINDOWS_PATH: Final = re.compile(r"[A-Za-z]:\\[^\s\"'<>]*")
_POSIX_PATH: Final = re.compile(r"(?<![\w<>~.])/(?:[^\s/\"'<>]+/)*[^\s/\"'<>]+")
_FILE_LINE: Final = re.compile(r'File "[^"]*"(?:, line \d+)?')
_CREDENTIAL: Final = (
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(token|password|passwd|secret|api[_-]?key)\s*[=:]\s*[^\s&;,]+"),
)
REDACTED_PATH: Final = "<路径>"
REDACTED_SECRET: Final = "<凭据>"
_TRACEBACK_MARK: Final = "Traceback (most recent call last)"


def message_safe(text: str) -> str:
    """把一条 `message` 擦到可以进帧的程度（幂等：擦过的串再擦一遍不变）。

    只做四件事，都宁可多擦一点：堆栈尾巴、绝对路径、凭据取值。**不做** HTML 转义——
    那是渲染层的事，帧是 JSON。
    """
    if not isinstance(text, str):  # 防御：调用方若传了非串，别让编码层崩
        return ""
    if _TRACEBACK_MARK in text:
        text = text.split(_TRACEBACK_MARK, 1)[0]
    text = _FILE_LINE.sub("", text)
    text = _HOME_PATH.sub(REDACTED_PATH, text)
    text = _WINDOWS_PATH.sub(REDACTED_PATH, text)
    text = _POSIX_PATH.sub(REDACTED_PATH, text)
    for pattern in _CREDENTIAL:
        if pattern.groups:
            text = pattern.sub(lambda m: f"{m.group(1)}=<凭据>", text)
        else:
            text = pattern.sub(REDACTED_SECRET, text)
    return " ".join(text.split())


def user_text(code: str) -> str:
    """码 → 文案；表外码回 `UNKNOWN_CODE_TEXT`。"""
    return ERROR_TEXT.get(code, UNKNOWN_CODE_TEXT)
