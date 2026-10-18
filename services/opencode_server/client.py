"""Async HTTP client for the OpenCode v1 HTTP API (``opencode serve``).

Contract source: ``sdd/flet-gui/opencode-api-contract`` (verified against
OpenCode 1.19.1 on Windows).

Design notes:
  - One ``httpx.AsyncClient`` with Basic auth user ``opencode``.
  - Every request carries ``?directory=<absolute posix path>``.
  - Idempotent GETs retry twice (0.5s / 1s). Prompts and permission replies
    NEVER retry blindly: a duplicate prompt or reply has real side effects.
  - ``stream_events()`` parses SSE framing (``data:`` lines, blank-line
    delimited), drops heartbeats and plumbing events, and yields only
    ``{"id", "type", "properties"}``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import httpx

from services.opencode_server.errors import (
    OpenCodeAuthError,
    OpenCodeRequestError,
    OpenCodeServerError,
    OpenCodeUnavailableError,
)
from services.permissions import PermissionRequest, Reply
from utils.logging import get_module_logger

logger = get_module_logger(__name__)

AUTH_USERNAME = "opencode"
GET_ATTEMPTS = 3  # first try + 2 retries
_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0)
_SSE_TIMEOUT = httpx.Timeout(30.0, connect=2.0, read=None)

# Events that are pure transport noise: on connect the server floods
# plugin/catalog/reference/integration updates and heartbeats. If these are
# not dropped, they consume the backend's event window before
# ``permission.asked`` ever arrives.
_FILTERED_TYPES = frozenset({"server.connected", "server.heartbeat"})
_FILTERED_PREFIXES = ("plugin.added", "catalog.", "reference.", "integration.")


class OpenCodeServerClient:
    """Thin typed wrapper around the OpenCode v1 endpoints."""

    def __init__(
        self,
        base_url: str,
        password: str,
        directory: str | Path,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._directory = Path(directory).as_posix()
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=httpx.BasicAuth(AUTH_USERNAME, password),
            timeout=httpx.Timeout(timeout),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ── Endpoints ──────────────────────────────────────────────────────────

    async def health(self) -> bool:
        """Return True when ``GET /global/health`` answers 200."""
        try:
            response = await self._get("/global/health")
            return response.status_code == 200
        except OpenCodeServerError:
            return False

    async def create_session(self, title: str) -> str:
        """Create a session and return its server-assigned id."""
        response = await self._request("POST", "/session", json={"title": title})
        data = self._json_object(response)
        session_id = data.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise OpenCodeUnavailableError("El servidor no devolvió un id de sesión válido.")
        return session_id

    async def send_prompt(
        self,
        session_id: str,
        prompt: str,
        *,
        model: str,
        agent: str | None = None,
    ) -> None:
        """Queue an async prompt (``POST /session/{id}/prompt_async`` -> 204)."""
        body: dict[str, Any] = {
            "parts": [{"type": "text", "text": prompt}],
            "model": self._split_model(model),
        }
        if agent:
            body["agent"] = agent
        await self._request("POST", f"/session/{session_id}/prompt_async", json=body)

    async def get_messages(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return the session's messages (oldest first, as the server sends)."""
        response = await self._get(
            f"/session/{session_id}/message",
            params={"limit": limit},
        )
        raw: object = response.json()
        if not isinstance(raw, list):
            raise OpenCodeUnavailableError("Respuesta inválida de /session/{id}/message.")
        messages: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise OpenCodeUnavailableError("Mensaje inválido en la respuesta del servidor.")
            messages.append(cast(dict[str, Any], item))
        return messages

    async def list_permissions(self) -> list[PermissionRequest]:
        """Return the pending permission requests (``GET /permission``)."""
        response = await self._get("/permission")
        raw: object = response.json()
        if not isinstance(raw, list):
            raise OpenCodeUnavailableError("Respuesta inválida de /permission.")
        return [self._parse_permission(item) for item in raw if isinstance(item, dict)]

    async def reply_permission(
        self,
        request_id: str,
        reply: Reply,
        message: str | None = None,
    ) -> bool:
        """Answer a permission request (``POST /permission/{id}/reply``)."""
        body: dict[str, Any] = {"reply": reply}
        if message is not None:
            body["message"] = message
        response = await self._request("POST", f"/permission/{request_id}/reply", json=body)
        try:
            return response.json() is True
        except ValueError as exc:
            raise OpenCodeUnavailableError("Respuesta inválida de /permission/{id}/reply.") from exc

    async def abort(self, session_id: str) -> bool:
        """Best-effort abort (endpoint tentative; 404 means 'not supported').

        Returns False instead of raising on 404 so callers can fall back to
        closing the SSE stream and documenting that the turn may finish.
        """
        try:
            await self._request("POST", f"/session/{session_id}/abort")
        except OpenCodeRequestError as exc:
            if exc.status_code == 404:
                return False
            raise
        return True

    async def stream_events(
        self,
        *,
        on_connect: Callable[[], None] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield filtered SSE events as ``{"id", "type", "properties"}``.

        SSE framing is respected: consecutive ``data:`` lines belong to one
        event and are joined with newlines; an empty line dispatches it.
        Comment lines (``: heartbeat``) and blank lines are ignored.

        ``on_connect`` fires once the HTTP response headers have arrived (the
        connection is established) but before any event is consumed. The
        backend uses it to guarantee the stream is open *before* sending a
        prompt, closing the verified ``permission.asked`` race.
        """
        async with self._client.stream(
            "GET",
            "/event",
            params=self._params(),
            timeout=_SSE_TIMEOUT,
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                self._raise_response_error(response)

            if on_connect is not None:
                on_connect()

            data_lines: list[str] = []
            async for line in response.aiter_lines():
                if line == "":
                    event = self._dispatch_event(data_lines)
                    data_lines = []
                    if event is not None:
                        yield event
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[len("data:") :].lstrip())

            event = self._dispatch_event(data_lines)
            if event is not None:
                yield event

    # ── Internals ──────────────────────────────────────────────────────────

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"directory": self._directory}
        if extra:
            params.update(extra)
        return params

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Single request, typed errors, no automatic retries."""
        try:
            response = await self._client.request(
                method,
                path,
                params=self._params(params),
                json=json,
            )
        except httpx.HTTPError as exc:
            raise OpenCodeUnavailableError(
                f"No se pudo conectar con el servidor OpenCode ({method} {path}): {exc}"
            ) from exc

        if response.status_code >= 400:
            self._raise_response_error(response)
        return response

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Idempotent GET: retry transport/5xx failures twice."""
        last_error: OpenCodeUnavailableError | None = None
        for attempt in range(GET_ATTEMPTS):
            try:
                return await self._request("GET", path, params=params)
            except OpenCodeUnavailableError as exc:
                last_error = exc
                if attempt < len(_RETRY_DELAYS):
                    delay = _RETRY_DELAYS[attempt]
                    logger.debug(
                        "Retrying OpenCode GET",
                        extra={
                            "event": "opencode_client_retry",
                            "path": path,
                            "attempt": attempt + 1,
                            "delay_s": delay,
                        },
                    )
                    await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error

    def _raise_response_error(self, response: httpx.Response) -> None:
        status = response.status_code
        body = response.text[:300]
        if status == 401:
            raise OpenCodeAuthError(
                "El servidor OpenCode rechazó las credenciales (401). "
                "Revisa OPENCODE_SERVER_PASSWORD."
            )
        if 400 <= status < 500:
            raise OpenCodeRequestError(
                f"El servidor OpenCode rechazó la petición ({status}).",
                status_code=status,
                body=body,
            )
        raise OpenCodeUnavailableError(
            f"El servidor OpenCode falló ({status}).",
            status_code=status,
        )

    def _json_object(self, response: httpx.Response) -> dict[str, Any]:
        try:
            raw: object = response.json()
        except ValueError as exc:
            raise OpenCodeUnavailableError("El servidor devolvió JSON inválido.") from exc
        if not isinstance(raw, dict):
            raise OpenCodeUnavailableError("El servidor devolvió un objeto JSON inesperado.")
        return cast(dict[str, Any], raw)

    @staticmethod
    def _split_model(model: str) -> dict[str, str]:
        """Split ``provider/model`` at the FIRST slash into the v1 model ref."""
        provider, separator, model_id = model.partition("/")
        if not separator or not provider or not model_id:
            raise ValueError(
                f"Modelo inválido: {model!r}. Se esperaba el formato 'provider/model'."
            )
        return {"providerID": provider, "modelID": model_id}

    @staticmethod
    def _parse_permission(raw: dict[str, Any]) -> PermissionRequest:
        request_id = raw.get("id")
        if not isinstance(request_id, str) or not request_id:
            raise OpenCodeUnavailableError("Solicitud de permiso sin id.")

        tool = raw.get("tool")
        tool_data = cast(dict[str, Any], tool) if isinstance(tool, dict) else {}
        metadata = raw.get("metadata")
        metadata_data = cast(dict[str, Any], metadata) if isinstance(metadata, dict) else {}

        return PermissionRequest(
            id=request_id,
            session_id=_optional_str(raw.get("sessionID")) or "",
            permission=_optional_str(raw.get("permission")) or "",
            patterns=_str_tuple(raw.get("patterns")),
            metadata=metadata_data,
            always=_str_tuple(raw.get("always")),
            message_id=_optional_str(tool_data.get("messageID")),
            call_id=_optional_str(tool_data.get("callID")),
        )

    @staticmethod
    def _dispatch_event(data_lines: list[str]) -> dict[str, Any] | None:
        if not data_lines:
            return None
        payload = "\n".join(data_lines)
        try:
            raw: object = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning(
                "Unparseable SSE event dropped",
                extra={"event": "opencode_sse_parse_error"},
            )
            return None
        if not isinstance(raw, dict):
            return None

        event = cast(dict[str, Any], raw)
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            return None
        if event_type in _FILTERED_TYPES or event_type.startswith(_FILTERED_PREFIXES):
            return None

        properties = event.get("properties")
        return {
            "id": _optional_str(event.get("id")) or "",
            "type": event_type,
            "properties": cast(dict[str, Any], properties) if isinstance(properties, dict) else {},
        }


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))
