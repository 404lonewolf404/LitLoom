"""Compatibility service binding for the modular monolith.

Domain modules resolve runtime dependencies through the application facade so existing
imports, tests, and extension points continue to observe patched services. New domain
code should prefer explicit arguments and ports.
"""

from __future__ import annotations

from types import ModuleType

_application: ModuleType | None = None


def bind_application(application: ModuleType) -> None:
    global _application
    _application = application


def app_services() -> ModuleType:
    if _application is None:
        raise RuntimeError("LitLoom application services have not been initialized")
    return _application
