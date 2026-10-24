"""Native FastAPI routes for model and Agent capabilities."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from types import ModuleType
from typing import Any

from fastapi import APIRouter, Body, FastAPI, Request
from fastapi.responses import JSONResponse


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": str(message)[:1000]}, status_code=status)


def _actor(application: ModuleType, request: Request) -> dict[str, Any] | None:
    """Resolve the current member without creating a second auth system."""
    if not application.team_auth_enabled():
        return None
    session = request.cookies.get("litloom_session", "")
    actor = application.resolve_auth_session(session)
    return actor or None


def _csrf_valid(application: ModuleType, request: Request, actor: dict[str, Any]) -> bool:
    cookie = request.cookies.get("litloom_csrf", "")
    header = request.headers.get("X-LitLoom-CSRF", "")
    return bool(
        cookie
        and header
        and application.constant_time_token_match(cookie, header)
        and application.constant_time_token_match(
            application.token_digest(cookie), actor.get("csrf_hash", "")
        )
    )


@contextlib.contextmanager
def _request_scope(application: ModuleType, request: Request, *, write: bool = False) -> Iterator[dict[str, Any] | None]:
    actor = _actor(application, request)
    if application.team_auth_enabled() and actor is None:
        raise PermissionError("请先登录团队工作区")
    if write and actor is not None and not _csrf_valid(application, request, actor):
        raise PermissionError("安全令牌已失效，请刷新页面后重试")
    token = application.AUTH_ACTOR.set(actor)
    try:
        yield actor
    finally:
        application.AUTH_ACTOR.reset(token)


def register_native_routes(
    web_app: FastAPI, application: ModuleType, *, runtime_managed: bool = True
) -> None:
    """Register routes that are fully owned by FastAPI.

    The function is intentionally called from the app factory so tests can
    inject the existing application facade without importing global state.
    """
    system = APIRouter(prefix="/api", tags=["system"])
    model = APIRouter(prefix="/api/settings/model", tags=["model"])
    agent = APIRouter(prefix="/api/agent", tags=["agent"])

    @system.get("/fastapi/health")
    async def fastapi_health() -> dict[str, Any]:
        return {
            "status": "ok",
            "framework": "fastapi",
            "bridge": "legacy-route-compatibility",
            "runtime_managed": runtime_managed,
        }

    @model.get("")
    async def get_model_settings(request: Request):
        try:
            with _request_scope(application, request):
                return application.model_settings()
        except PermissionError as exc:
            return _error(str(exc), 401)

    @model.get("/providers")
    async def get_model_providers(request: Request):
        try:
            with _request_scope(application, request):
                return {"providers": application.model_provider_presets()}
        except PermissionError as exc:
            return _error(str(exc), 401)

    @model.patch("")
    async def update_model_settings(request: Request, payload: dict[str, Any] = Body(default={})):  # noqa: B008
        try:
            with _request_scope(application, request, write=True):
                return application.save_model_settings(payload)
        except PermissionError as exc:
            return _error(str(exc), 401 if "登录" in str(exc) else 403)
        except (ValueError, TypeError) as exc:
            return _error(str(exc), 400)

    @model.post("/test")
    async def test_model(request: Request, payload: dict[str, Any] = Body(default={})):  # noqa: B008
        try:
            with _request_scope(application, request, write=True):
                deep = bool(payload.get("deep", True))
                override = payload.get("settings")
                if isinstance(override, dict):
                    return await asyncio.to_thread(
                        application.test_model_connection,
                        deep=deep,
                        settings_override=override,
                    )
                return await asyncio.to_thread(application.test_model_connection, deep=deep)
        except PermissionError as exc:
            return _error(str(exc), 401 if "登录" in str(exc) else 403)
        except (application.HTTPError, application.URLError, TimeoutError, OSError, ValueError) as exc:
            return _error(f"模型连接测试失败：{str(exc)[:500]}", 502)

    @agent.get("/framework")
    async def get_agent_framework(request: Request):
        try:
            with _request_scope(application, request):
                return application.agent_framework_status()
        except PermissionError as exc:
            return _error(str(exc), 401)

    @agent.get("/status")
    async def get_agent_status(request: Request):
        try:
            with _request_scope(application, request):
                settings = application.model_settings()
                return {
                    "configured": settings["api_key_set"],
                    "framework": application.agent_framework_status(),
                    **settings,
                }
        except PermissionError as exc:
            return _error(str(exc), 401)

    @agent.get("/runs/{run_id}")
    async def get_agent_run(run_id: str, request: Request):
        try:
            with _request_scope(application, request):
                run = application.agent_run_snapshot(run_id)
                if not run:
                    return _error("Agent 运行不存在", 404)
                return run
        except PermissionError as exc:
            return _error(str(exc), 401)

    @agent.post("/autonomous")
    async def run_autonomous_agent(request: Request, payload: dict[str, Any] = Body(default={})):  # noqa: B008
        try:
            with _request_scope(application, request, write=True):
                return await asyncio.to_thread(application.autonomous_agent, payload)
        except PermissionError as exc:
            return _error(str(exc), 401 if "登录" in str(exc) else 403)
        except (ValueError, RuntimeError) as exc:
            return _error(str(exc), 400)

    web_app.include_router(system)
    web_app.include_router(model)
    web_app.include_router(agent)


__all__ = ["register_native_routes"]
