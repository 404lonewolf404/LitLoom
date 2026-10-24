"""FastAPI application boundary for LitLoom.

The domain modules and the mature HTTP handler are kept independent from the
ASGI framework.  During the migration, FastAPI owns the public socket and
lifespan while a loopback-only compatibility server handles legacy routes.
This keeps existing browser/API contracts stable and gives new endpoints a
normal FastAPI home without duplicating every historical route at once.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from http.server import ThreadingHTTPServer
from types import ModuleType
from typing import Any

from ..api import register_native_routes

try:
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import Response
except ImportError as exc:  # pragma: no cover - exercised by minimal installs
    httpx = None
    FastAPI = None
    Request = Any
    Response = Any
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class LegacyRouteBridge:
    """Own the short-lived local HTTP server used during route migration."""

    def __init__(self, application: ModuleType):
        self.application = application
        self.server: ThreadingHTTPServer | None = None
        self.thread = None

    def start(self) -> None:
        handler = self.application.Handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = self.application.threading.Thread(
            target=self.server.serve_forever,
            name="litloom-legacy-route-bridge",
            daemon=True,
        )
        self.thread.start()

    @property
    def url(self) -> str:
        if self.server is None:
            raise RuntimeError("FastAPI legacy bridge 尚未启动")
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        server, thread = self.server, self.thread
        self.server = None
        self.thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)


def _forward_request_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        lowered = name.casefold()
        if lowered in _HOP_BY_HOP_HEADERS or lowered in {"content-length", "host"}:
            continue
        headers[name] = value
    # The legacy auth layer validates Origin against Host. Keep the browser's
    # public host while the TCP connection itself targets the random bridge.
    if request.headers.get("host"):
        headers["Host"] = request.headers["host"]
    return headers


async def _proxy_to_legacy(
    request: Request,
    bridge: LegacyRouteBridge,
    client: httpx.AsyncClient,
) -> Response:
    body = await request.body()
    target = bridge.url + request.url.path
    if request.url.query:
        target += "?" + request.url.query
    upstream = await client.request(
        request.method,
        target,
        content=body,
        headers=_forward_request_headers(request),
    )
    response = Response(content=upstream.content, status_code=upstream.status_code)
    for name, value in upstream.headers.multi_items():
        lowered = name.casefold()
        if lowered in _HOP_BY_HOP_HEADERS or lowered in {"content-length", "content-encoding"}:
            continue
        response.headers.append(name, value)
    return response


def create_app(
    application: ModuleType | None = None,
    *,
    manage_runtime: bool = True,
) -> Any:
    """Create the public FastAPI application.

    ``manage_runtime=False`` is useful for isolated ASGI tests that already
    prepared a temporary database. Production uses the default lifecycle,
    which starts PostgreSQL-backed workers before accepting requests.
    """
    if _IMPORT_ERROR is not None:
        raise RuntimeError(
            "FastAPI 运行依赖未安装，请运行 python -m pip install -e ."
        ) from _IMPORT_ERROR
    if application is None:
        import app as application  # type: ignore[no-redef]

    bridge = LegacyRouteBridge(application)
    client: httpx.AsyncClient | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal client
        if manage_runtime:
            await asyncio.to_thread(application.startup_runtime)
        bridge.start()
        client = httpx.AsyncClient(timeout=None, trust_env=False)
        _app.state.legacy_bridge = bridge
        _app.state.runtime_managed = manage_runtime
        try:
            yield
        finally:
            await client.aclose()
            client = None
            bridge.stop()
            if manage_runtime:
                await asyncio.to_thread(application.shutdown_runtime)

    web_app = FastAPI(
        title="LitLoom",
        version=str(application.APP_VERSION),
        description="面向研究生的可审计论文检索、阅读与 Agent 工作台",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    register_native_routes(web_app, application, runtime_managed=manage_runtime)

    @web_app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    async def legacy_routes(request: Request, path: str) -> Response:
        if client is None:
            raise RuntimeError("FastAPI legacy bridge 尚未完成启动")
        return await _proxy_to_legacy(request, bridge, client)

    return web_app


if FastAPI is not None:
    # Importing the module exposes the conventional ASGI target:
    # ``uvicorn literature_desk.web.fastapi_app:app``.
    app = create_app()
else:  # pragma: no cover - only for an intentionally minimal installation
    app = None


__all__ = ["app", "create_app", "LegacyRouteBridge"]
