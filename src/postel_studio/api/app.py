"""路由表：S1 的 `/` 与 `/api/health`，S2 挂上网关 `/gateway/<路径令牌>`。

页面与内容原本是常量、**不依赖请求可变的变量**，故一次性构造。S2 引入的例外只有一处：
`/` 每渲染一次就签发一个**一次性 handshake ticket**（60 秒、用后即删，见 05 §1.1 的 S2 修订），
所以 `render_index` 每次请求都要跑一遍。页面里**不放**连接令牌，只有这个 ticket；
页面自身仍不需要认证（只含版本、状态根与一个一次性随机串）。
"""

from __future__ import annotations

import os
from typing import Final

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from postel_studio import __version__
from postel_studio.api import gateway
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


def create_app(ws_path: str = gateway.GATEWAY_PATH) -> FastAPI:
    """构造内核的 HTTP 应用。

    `ws_path` 由 `server.py` 生成（`/gateway/<路径令牌>`）并写进 `server.json` 的 `wsPath`：
    浏览器不需要它（从页面 meta 的 ticket 换连接），非浏览器客户端据它拼 URL。
    """
    app = FastAPI(title="Postel Studio Kernel", docs_url=None, redoc_url=None, openapi_url=None)
    tickets = gateway.TicketRegistry()
    root = str(state_root())

    def index() -> HTMLResponse:
        page = render_index(
            version=__version__,
            api_version=API_VERSION,
            state_root=root,
            handshake_ticket=tickets.issue(),
            ws_path=ws_path,
        )
        return HTMLResponse(
            page, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        )

    app.get("/", response_class=HTMLResponse)(index)

    @app.get("/api/health")
    def health_route() -> dict[str, object]:
        return health()

    gateway.install(app, ws_path, tickets)
    return app
