"""LangChain Runnable adapter around LitLoom's observable model transport."""

from __future__ import annotations

from typing import Any

from ..services import app_services as _services

try:
    from langchain_core.runnables import RunnableLambda
except ImportError:  # pragma: no cover - minimal maintenance installs
    RunnableLambda = None


def _invoke_transport(request: dict[str, Any]) -> dict[str, Any]:
    endpoint = request["endpoint"]
    payload = request["payload"]
    api_key = request["api_key"]
    protocol = request["protocol"]
    with _services().model_usage_context(request.get("task", "autonomous_agent"), "text"):
        with _services().open_compatible_model_request(endpoint, payload, api_key, 120) as response:
            result = _services().read_model_response(response)
    return {"response": result, "protocol": protocol}


def create_agent_llm():
    """Return a LangChain Runnable while retaining LitLoom telemetry/retries."""
    if RunnableLambda is None:
        return None
    return RunnableLambda(_invoke_transport)


__all__ = ["create_agent_llm"]
