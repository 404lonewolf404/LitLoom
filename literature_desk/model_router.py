"""Task-level model selection for the shared OpenAI-compatible endpoint."""

from __future__ import annotations


TASK_MODEL_FIELDS = {
    "fast": "fast_model",
    "analysis": "analysis_model",
    "vision": "vision_model",
}


def resolve_model_route(settings: dict, task: str = "analysis") -> dict:
    """Return a copy of settings with the configured task model selected.

    Empty route fields intentionally inherit the primary model so existing
    installations keep their current behavior until they opt into routing.
    """
    key = TASK_MODEL_FIELDS.get(task)
    route_model = str(settings.get(key) or "").strip() if key else ""
    selected = route_model or str(settings.get("model") or "").strip()
    route = task if route_model else "default"
    return {**settings, "model": selected, "model_route": route}
