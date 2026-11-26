"""全局夹具与测试隔离兜底。

**硬约束**：测试绝不能碰真实的 `~/.postel`（S1 禁止事项）。两道锁：

1. `POSTEL_HOME` 被指到会话级临时目录：即使某个用例忘了用 `home` 夹具，也只会写在
   `/tmp` 下的会话目录里。
2. `Path.home` 同样改指那里：`state_root()` 的缺省分支（`~/.postel`）也落不进真实 HOME。

会话结束后由 pytest 清理临时目录；内核子进程若还有残留，属实现 bug（集成测试里已有断言）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_session_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """会话级隔离：把 `POSTEL_HOME` 与 `HOME` 都指到临时目录。"""
    sandbox = tmp_path_factory.mktemp("session-home")
    os.environ["POSTEL_HOME"] = str(sandbox)
    os.environ["HOME"] = str(sandbox)
    # 已 import 的模块不该缓存旧值：本项目所有路径都在调用时读环境变量，这里只是显式声明假设
    Path.home = classmethod(lambda cls: sandbox)  # type: ignore[assignment,method-assign]
    return sandbox


@pytest.fixture
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """单个用例的独立状态根（比会话级更严）。"""
    root = tmp_path / "postel-home"
    monkeypatch.setenv("POSTEL_HOME", str(root))
    return root
