"""Privacy-preserving helpers for OpenAI-compatible model telemetry."""

from __future__ import annotations

from urllib.parse import urlsplit


def response_usage(result: dict) -> dict:
    """Normalize common Chat Completions and Responses usage fields."""
    usage = result.get("usage") if isinstance(result, dict) else None
    if not isinstance(usage, dict):
        usage = result.get("_litloom_usage") if isinstance(result, dict) else None
    if not isinstance(usage, dict):
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None, "cost_usd": None}

    def integer(*keys):
        for key in keys:
            value = usage.get(key)
            if value is None:
                continue
            try:
                return max(0, int(value))
            except (TypeError, ValueError, OverflowError):
                continue
        return None

    def money(*keys):
        for key in keys:
            value = usage.get(key)
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if number >= 0:
                return number
        return None

    input_tokens = integer("prompt_tokens", "input_tokens")
    output_tokens = integer("completion_tokens", "output_tokens")
    total_tokens = integer("total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    cost = money("cost_usd", "total_cost", "cost")
    if cost is None and isinstance(usage.get("cost_details"), dict):
        cost = money_from_dict(usage["cost_details"], "upstream_inference_cost", "total_cost", "cost")
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens, "cost_usd": cost}


def money_from_dict(value: dict, *keys) -> float | None:
    for key in keys:
        try:
            amount = float(value.get(key))
        except (TypeError, ValueError, OverflowError):
            continue
        if amount >= 0:
            return amount
    return None


def endpoint_host(endpoint: str) -> str:
    """Keep only a normalized host; custom endpoint paths may contain secrets."""
    try:
        parsed = urlsplit(str(endpoint or ""))
        host = (parsed.hostname or "").casefold()
        port = parsed.port
        if port and not ((parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)):
            host = f"{host}:{port}"
        return host[:180]
    except (TypeError, ValueError):
        return ""


def payload_text_chars(value, parent_key: str = "") -> int:
    """Count text size without counting image URLs/base64 or retaining text."""
    if isinstance(value, str):
        if parent_key in {"url", "image_url", "data", "data_url", "type", "role", "model", "detail", "stream"}:
            return 0
        return len(value)
    if isinstance(value, list):
        return sum(payload_text_chars(item, parent_key) for item in value)
    if isinstance(value, dict):
        return sum(payload_text_chars(item, str(key)) for key, item in value.items())
    return 0
