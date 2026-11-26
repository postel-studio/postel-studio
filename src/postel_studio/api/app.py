"""路由表：S1 只开两个路由，`/` 与 `/api/health`（见 docs/design/issues/S1-kernel-visible.md）。

页面与内容都是常量，**不依赖请求可变的变量**，故一次性构造，避免每个请求重复拼接。
`/` 只展示版本、状态根、apiVersion——不含任何凭据（05-security §4.1）。
"""

from __future__ import annotations

import os
from typing import Final

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from postel_studio import __version__
from postel_studio.storage.paths import state_root
from postel_studio.web.assets import render_index

API_VERSION: Final = "1"


def health() -> dict[str, object]:
    """内核存活应答。字段与 07-api-contract §2.1 `system.health` 对齐。"""
    return {
        "status": "ok",
        "version": __version__,
        "apiVersion": API_VERSION,
        "stateRoot": str(state_root()),
        "pid": os.getpid(),
    }


def create_app() -> FastAPI:
    """构造内核的 HTTP 应用。"""
    app = FastAPI(title="Postel Studio Kernel", docs_url=None, redoc_url=None, openapi_url=None)
    root = str(state_root())
    page = render_index(version=__version__, api_version=API_VERSION, state_root=root)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(
            page, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        )

    @app.get("/api/health")
    def health_route() -> dict[str, object]:
        return health()

    return app
