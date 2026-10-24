"""HTTP request handling composed from focused route and workflow mixins."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler

from .handler_core import _HandlerCoreMixin
from .handler_read import _HandlerReadMixin
from .handler_write import _HandlerWriteMixin
from .handler_library_io import _HandlerLibraryIOMixin
from .handler_files import _HandlerFilesMixin
from .handler_ai import _HandlerAIMixin


class Handler(
    _HandlerCoreMixin,
    _HandlerReadMixin,
    _HandlerWriteMixin,
    _HandlerLibraryIOMixin,
    _HandlerFilesMixin,
    _HandlerAIMixin,
    BaseHTTPRequestHandler,
):
    """Compatibility HTTP handler backed by feature-focused mixins."""

    server_version = "LitLoom"


__all__ = ["Handler"]
