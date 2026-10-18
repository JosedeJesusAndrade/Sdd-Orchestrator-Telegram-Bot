"""OpenCode server lifecycle manager and HTTP client (v1 API)."""

from __future__ import annotations

from services.opencode_server.client import OpenCodeServerClient
from services.opencode_server.errors import (
    OpenCodeAuthError,
    OpenCodeRequestError,
    OpenCodeServerConfigError,
    OpenCodeServerError,
    OpenCodeUnavailableError,
)
from services.opencode_server.manager import OpenCodeServerManager, ServerConfig

__all__ = [
    "OpenCodeAuthError",
    "OpenCodeRequestError",
    "OpenCodeServerClient",
    "OpenCodeServerConfigError",
    "OpenCodeServerError",
    "OpenCodeServerManager",
    "OpenCodeUnavailableError",
    "ServerConfig",
]
