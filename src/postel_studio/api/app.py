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
from postel_studio.api.repository import RepositoryService
from postel_studio.storage.db import RepoStore
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


def create_app(
    ws_path: str = gateway.GATEWAY_PATH, *, host: str = "127.0.0.1", port: int = 0
) -> FastAPI:
    """构造内核的 HTTP 应用。

    `ws_path` 由 `server.py` 生成（`/gateway/<状态根哈希>`）并写进 `server.json` 的 `wsPath`。
    `host`/`port` 是**这个进程真实在听的地址**：`/api/where` 回显它们，前端重连时据此判断
    自己是不是还连着同一个内核（D7）。默认值只给测试/进程内集成用，`server.py` 总是显式传。
    """
    app = FastAPI(title="Postel Studio Kernel", docs_url=None, redoc_url=None, openapi_url=None)
    tickets = gateway.TicketRegistry()
    root = str(state_root())
    # S3/S4：内核持有用户数据。store 与 gitops 在这里构造、**作为能力注入**给网关；
    # 网关因此不需要 import 任何业务/存储模块（06-pitfalls T14 的修法，别改回反向 import）。
    store = RepoStore()
    repositories = RepositoryService(store)

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

    @app.get("/api/where")
    def where_route() -> dict[str, object]:
        """内核自己的位置（D7）：`host`/`port`/`wsPath`/`pid`。

        **不读 `server.json`**——那份文件可能已被新内核覆盖，也可能已删；这里回的是**本进程
        真实在听的绑定**（由 `server.py` 传进来），所以「问到的地址」永远指向答话的这个内核。
        只含地址类信息，不含 token / ticket，故与 `/` 一样不需要认证。
        """
        return {"host": host, "port": port, "wsPath": ws_path, "pid": os.getpid()}

    # health() 在这里定义、注入给网关：网关不反向 import 本模块，环被消掉而不是被绕开。
    gateway.install(
        app,
        ws_path,
        tickets,
        gateway.Providers(
            health=health,
            repo_bind=repositories.bind,
            repo_list=repositories.list,
            repo_branches=repositories.branches,  # S4
        ),
    )
    return app
