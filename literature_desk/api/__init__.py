"""Native FastAPI API boundary for LitLoom.

The legacy handler remains available while routes are migrated in small,
tested slices. New endpoints belong here instead of growing the compatibility
handler.
"""

from .native import register_native_routes

__all__ = ["register_native_routes"]
