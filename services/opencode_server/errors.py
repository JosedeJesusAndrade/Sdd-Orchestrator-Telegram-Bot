"""Typed errors for the OpenCode server lifecycle and HTTP client.

Every failure mode of the server stack is expressed as one of these types so
callers (the manager supervisor, the backend, the entry points) can make
policy decisions without inspecting strings:

  - ``OpenCodeServerConfigError``   -> configuration is invalid (fail fast)
  - ``OpenCodeAuthError``           -> credentials rejected (never retry)
  - ``OpenCodeRequestError``        -> 4xx from the API (never retry)
  - ``OpenCodeUnavailableError``    -> transport failure / 5xx (may retry)

``OpenCodeServerError`` is the common base so a broad ``except`` can keep the
supervisor alive without swallowing unrelated programming errors.
"""

from __future__ import annotations


class OpenCodeServerError(Exception):
    """Base class for every OpenCode server error."""


class OpenCodeServerConfigError(OpenCodeServerError):
    """Invalid or incomplete server configuration (e.g. empty password)."""


class OpenCodeAuthError(OpenCodeServerError):
    """The server rejected the credentials (HTTP 401)."""


class OpenCodeRequestError(OpenCodeServerError):
    """The server rejected a request with a 4xx response.

    ``status_code`` and ``body`` are preserved so callers can log a useful
    diagnosis without re-reading the response (the client never includes the
    password or the Basic auth header in the body snippet).
    """

    def __init__(self, message: str, *, status_code: int, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class OpenCodeUnavailableError(OpenCodeServerError):
    """The server could not be reached, or answered with a 5xx.

    ``status_code`` is ``None`` for transport-level failures (connection
    refused, timeout) and the HTTP status for server-side failures.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
