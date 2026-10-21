"""Deterministic, opt-in release and canary assignment helpers."""

from __future__ import annotations

import hashlib
import os


def _percent(value: object, default: int = 0) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def _features() -> list[str]:
    raw = os.environ.get("LITLOOM_CANARY_FEATURES", "")
    return list(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


def feature_assignment(feature: str, subject: str = "local") -> dict:
    """Return a stable canary decision without storing or exposing identity.

    A feature is opted into only when listed in ``LITLOOM_CANARY_FEATURES``.
    The same subject stays in the same bucket across restarts, while the raw
    subject never leaves this process.
    """
    name = str(feature or "").strip() or "unnamed"
    stable_subject = str(subject or "local")
    percent = _percent(os.environ.get("LITLOOM_CANARY_PERCENT", "0"))
    enabled_feature = name in _features()
    salt = os.environ.get("LITLOOM_CANARY_SALT", "litloom-canary-v1")
    digest = hashlib.sha256(f"{salt}:{name}:{stable_subject}".encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 100
    enabled = bool(enabled_feature and percent > bucket)
    return {"feature": name, "enabled": enabled, "bucket": bucket, "percent": percent, "configured": enabled_feature}


def release_snapshot(subject: str = "local") -> dict:
    """Return safe release/channel metadata for diagnostics and operators."""
    channel = os.environ.get("LITLOOM_RELEASE_CHANNEL", "stable").strip().lower() or "stable"
    if channel not in {"stable", "canary", "development"}:
        channel = "stable"
    features = _features()
    assignments = {feature: feature_assignment(feature, subject) for feature in features}
    return {
        "channel": channel,
        "canary_percent": _percent(os.environ.get("LITLOOM_CANARY_PERCENT", "0")),
        "configured_features": features,
        "assignments": assignments,
        "subject_scoped": bool(subject),
    }


__all__ = ["feature_assignment", "release_snapshot"]
