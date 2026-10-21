"""Stable API error vocabulary for the modular monolith."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AppError(Exception):
    """An expected application failure with a stable machine-readable code."""

    code: str
    message: str
    status: int = 400
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def payload(self, *, request_id: str = "") -> dict[str, Any]:
        value: dict[str, Any] = {
            "error": self.message,
            "error_code": self.code,
            "retryable": self.retryable,
        }
        if request_id:
            value["request_id"] = request_id
        if self.details:
            value["details"] = self.details
        return value


_STATUS_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    502: "upstream_error",
    503: "service_unavailable",
    504: "upstream_timeout",
}


def status_error_code(status: int) -> str:
    """Map an HTTP status to a stable fallback code."""
    return _STATUS_CODES.get(int(status), "http_error")


def error_payload(
    error: Exception | str,
    status: int = 500,
    *,
    request_id: str = "",
    code: str = "",
    retryable: bool = False,
) -> dict[str, Any]:
    """Normalize expected and unexpected errors without leaking tracebacks."""
    if isinstance(error, AppError):
        return error.payload(request_id=request_id)
    message = str(error).strip() or "请求失败"
    return {
        "error": message[:700],
        "error_code": code or status_error_code(status),
        "retryable": bool(retryable),
        **({"request_id": request_id} if request_id else {}),
    }


__all__ = ["AppError", "error_payload", "status_error_code"]
