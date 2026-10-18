"""AIBackend implementation backed by the long-lived OpenCode HTTP server.

Why this exists: ``opencode run`` is a one-shot subprocess. It cannot answer
``permission.asked``, it blocks per prompt, and the session id only leaks out
through ``--format json``. This backend talks to ``opencode serve`` instead:

  1. The session id is explicit (created via ``POST /session``).
  2. The SSE stream is opened **before** the prompt is sent — a verified race:
     the server can emit ``permission.asked`` between ``prompt_async`` and the
     stream connect, and the request would be lost.
  3. Text arrives incrementally (``message.part.delta`` / ``...updated``) and
     is forwarded to ``on_progress`` (throttled, tail-only).
  4. ``permission.asked`` is delegated to an optional handler. Without one the
     backend FAILS CLOSED (replies ``reject``) so the agent never hangs while
     the real PermissionRouter (Phase 3) does not exist yet.
  5. The timeout is an **idle** deadline (reset by every event), not a
     wall-clock limit; waiting for a human must not consume the prompt budget.
     Setup steps (session create, SSE connect, prompt send) and the wait for
     the FIRST post-prompt event are bounded, so a silent stream fails fast
     with a typed error instead of hanging until the idle deadline.

  6. Every call is scoped to the CONVERSATION's ``workdir`` (``?directory=``).
     The server publishes sessions, events, and permissions per directory; a
     stream opened against another directory connects but never delivers, so
     the workdir is threaded through the whole execution — it is NOT ignored.

The SSE reconnect/backoff + event-id dedup live here (not in the HTTP client)
because only this layer knows which session it is waiting for and when it is
safe to stop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, NoReturn, cast

from services.ai_backend import AIBackendResult
from services.opencode_server.client import OpenCodeServerClient
from services.opencode_server.errors import (
    OpenCodeServerError,
    OpenCodeStreamError,
    OpenCodeUnavailableError,
)
from services.opencode_server.manager import OpenCodeServerManager
from services.permissions import PermissionDecision, PermissionRequest
from utils.logging import get_module_logger, log_exception

logger = get_module_logger(__name__)

PermissionHandler = Callable[[PermissionRequest], Awaitable[PermissionDecision]]

CONNECT_WAIT = 2.0
REQUEST_TIMEOUT = 30.0
FIRST_EVENT_TIMEOUT = 30.0
DEDUP_LIMIT = 512
DEFAULT_PROGRESS_INTERVAL = 0.5
DEFAULT_PROGRESS_TAIL = 600
DEFAULT_RECONNECT_INITIAL = 0.5
DEFAULT_RECONNECT_MAX = 5.0
PHASE3_REJECT_MESSAGE = "Las aprobaciones se habilitan en la Fase 3."

_STREAM_END = object()


@dataclass(frozen=True)
class _PermissionEvent:
    """A permission request reconstructed from ``GET /permission``."""

    request: PermissionRequest


@dataclass(frozen=True)
class _StreamFailure:
    """A stream failure; the pump reconnects after reporting it."""

    error: Exception


class _ProgressEmitter:
    """Throttled, tail-only ``on_progress`` bridge."""

    def __init__(
        self,
        callback: Callable[[str], Awaitable[None]] | None,
        interval: float,
        tail: int,
    ) -> None:
        self._callback = callback
        self._interval = interval
        self._tail = tail
        self._last = 0.0

    async def emit(self, text: str) -> None:
        if self._callback is None or not text:
            return
        now = time.monotonic()
        if now - self._last < self._interval:
            return
        self._last = now
        try:
            await self._callback(text[-self._tail :])
        except Exception:
            log_exception(
                "opencode_progress_callback_failed",
                module=__name__,
                level=logging.DEBUG,
            )


class OpenCodeServerBackend:
    """Streams a prompt through ``opencode serve`` and returns the final text."""

    #: ``execute`` pushes incremental text through ``on_progress``.
    supports_live_progress = True

    def __init__(
        self,
        client: OpenCodeServerClient,
        *,
        permission_handler: PermissionHandler | None = None,
        timeout: int = 300,
        manager: OpenCodeServerManager | None = None,
        progress_interval: float = DEFAULT_PROGRESS_INTERVAL,
        progress_tail: int = DEFAULT_PROGRESS_TAIL,
        reconnect_initial: float = DEFAULT_RECONNECT_INITIAL,
        reconnect_max: float = DEFAULT_RECONNECT_MAX,
        connect_timeout: float = CONNECT_WAIT,
        request_timeout: float = REQUEST_TIMEOUT,
        first_event_timeout: float = FIRST_EVENT_TIMEOUT,
    ) -> None:
        self._client = client
        self._permission_handler = permission_handler
        self._timeout = timeout
        self._manager = manager
        self._manager_ready = False
        self._progress_interval = progress_interval
        self._progress_tail = progress_tail
        self._reconnect_initial = reconnect_initial
        self._reconnect_max = reconnect_max
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout
        self._first_event_timeout = first_event_timeout

        self._cancelled = False
        self._active_session_id: str | None = None
        self._active_directory: str | None = None
        self._queue: asyncio.Queue[object] | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._abort_task: asyncio.Task[None] | None = None
        self._progress: _ProgressEmitter | None = None

    async def execute(
        self,
        prompt: str,
        model: str,
        session_id: str | None,
        agent: str | None = None,
        workdir: str | None = None,
        source_label: str = "",
        timeout: int | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> AIBackendResult:
        """Run one prompt through the server and stream its output.

        ``workdir`` is the CONVERSATION's directory and scopes every call
        (session create, SSE stream, prompt, permissions, fallback, abort).
        The OpenCode server publishes events per ``?directory=``; sending the
        prompt and listening on different directories connects successfully
        but yields no events. When ``workdir`` is ``None`` the client's default
        directory is used.
        """
        effective_timeout = timeout if timeout is not None else self._timeout
        directory = workdir
        self._cancelled = False
        self._abort_task = None
        self._active_directory = directory
        self._progress = _ProgressEmitter(
            on_progress,
            self._progress_interval,
            self._progress_tail,
        )

        logger.info(
            "OpenCode prompt backend start",
            extra={
                "event": "opencode_prompt_backend_start",
                "session_id": session_id,
                "directory": directory or self._client.directory,
                "model": model,
                "agent": agent,
            },
        )

        try:
            if self._manager is not None and not self._manager_ready:
                await self._manager.ensure_running(directory=directory)
                self._manager_ready = True

            sid = session_id
            if sid is None:
                sid = await self._create_session(prompt, directory)
            self._active_session_id = sid

            if self._cancelled:
                return AIBackendResult(session_id=sid, cancelled=True)

            return await self._run_stream(
                session_id=sid,
                prompt=prompt,
                model=model,
                agent=agent,
                effective_timeout=effective_timeout,
                directory=directory,
            )
        except OpenCodeUnavailableError:
            # The server may have died: let the next prompt re-probe.
            self._manager_ready = False
            raise
        finally:
            self._progress = None
            if self._abort_task is not None:
                # Let the best-effort abort read the active ids: clearing them
                # first would turn a scheduled abort into a silent no-op.
                with contextlib.suppress(Exception):
                    await self._abort_task
                self._abort_task = None
            self._active_session_id = None
            self._active_directory = None

    async def _create_session(self, prompt: str, directory: str | None) -> str:
        """Create the session with a bounded wait (never hang silently)."""
        try:
            sid = await asyncio.wait_for(
                self._client.create_session(title=prompt[:60], directory=directory),
                timeout=self._request_timeout,
            )
        except TimeoutError as exc:
            logger.error(
                "OpenCode session creation timed out",
                extra={
                    "event": "opencode_session_create_timeout",
                    "directory": directory or self._client.directory,
                    "timeout_s": self._request_timeout,
                },
            )
            raise OpenCodeUnavailableError(
                f"El servidor OpenCode no creó la sesión en "
                f"{self._request_timeout:g}s (directory={directory or self._client.directory})."
            ) from exc

        logger.info(
            "OpenCode session created",
            extra={
                "event": "opencode_session_created",
                "session_id": sid,
                "directory": directory or self._client.directory,
            },
        )
        return sid

    def cancel(self) -> None:
        """Stop the active prompt: flag + close the stream + best-effort abort."""
        self._cancelled = True
        if self._queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(_STREAM_END)
        if self._pump_task is not None:
            self._pump_task.cancel()
        if self._abort_task is None and self._active_session_id is not None:
            try:
                self._abort_task = asyncio.create_task(self._abort())
            except RuntimeError:
                log_exception(
                    "opencode_abort_schedule_failed",
                    module=__name__,
                    level=logging.DEBUG,
                )

    async def _abort(self) -> None:
        sid = self._active_session_id
        if sid is None:
            return
        try:
            await self._client.abort(sid, directory=self._active_directory)
        except Exception:
            log_exception(
                "opencode_abort_failed",
                module=__name__,
                level=logging.WARNING,
            )

    # ── Stream orchestration ───────────────────────────────────────────────

    async def _run_stream(
        self,
        *,
        session_id: str,
        prompt: str,
        model: str,
        agent: str | None,
        effective_timeout: int,
        directory: str | None,
    ) -> AIBackendResult:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[object] = asyncio.Queue()
        connected = asyncio.Event()
        stop = asyncio.Event()
        self._queue = queue

        pump = asyncio.create_task(self._pump(session_id, queue, connected, stop, directory))
        self._pump_task = pump

        parts: dict[str, str] = {}
        seen_ids: set[str] = set()
        seen_order: deque[str] = deque()
        handled_permissions: set[str] = set()
        pending_permissions: set[asyncio.Task[None]] = set()
        last_error: Exception | None = None
        timed_out = False

        try:
            try:
                await asyncio.wait_for(connected.wait(), timeout=self._connect_timeout)
            except TimeoutError as exc:
                logger.error(
                    "SSE stream did not connect before the prompt",
                    extra={
                        "event": "opencode_sse_connect_timeout",
                        "session_id": session_id,
                        "directory": directory or self._client.directory,
                        "timeout_s": self._connect_timeout,
                    },
                )
                raise OpenCodeStreamError(
                    f"El stream SSE no se abrió en {self._connect_timeout:g}s "
                    f"(directory={directory or self._client.directory}).",
                    directory=directory,
                ) from exc

            logger.info(
                "OpenCode SSE stream connected",
                extra={
                    "event": "opencode_sse_connected",
                    "session_id": session_id,
                    "directory": directory or self._client.directory,
                },
            )

            try:
                await asyncio.wait_for(
                    self._client.send_prompt(
                        session_id,
                        prompt,
                        model=model,
                        agent=agent,
                        directory=directory,
                    ),
                    timeout=self._request_timeout,
                )
            except TimeoutError as exc:
                logger.error(
                    "OpenCode prompt send timed out",
                    extra={
                        "event": "opencode_prompt_send_timeout",
                        "session_id": session_id,
                        "directory": directory or self._client.directory,
                        "timeout_s": self._request_timeout,
                    },
                )
                raise OpenCodeUnavailableError(
                    f"El servidor OpenCode no aceptó el prompt en "
                    f"{self._request_timeout:g}s (session={session_id})."
                ) from exc

            logger.info(
                "OpenCode prompt sent",
                extra={
                    "event": "opencode_prompt_sent",
                    "session_id": session_id,
                    "directory": directory or self._client.directory,
                },
            )

            deadline = loop.time() + effective_timeout
            first_deadline = loop.time() + self._first_event_timeout
            saw_event = False
            while not self._cancelled:
                if pending_permissions:
                    # A human is deciding: do NOT let the prompt timeout run.
                    deadline = loop.time() + effective_timeout
                remaining = deadline - loop.time()
                if not saw_event:
                    # A healthy stream emits session/message events immediately.
                    # No event at all means the stream is scoped wrong or dead:
                    # fail fast instead of waiting out the (possibly 20 min)
                    # idle deadline in silence.
                    remaining = min(remaining, first_deadline - loop.time())
                if remaining <= 0:
                    if not saw_event:
                        self._raise_no_events(session_id, directory)
                    timed_out = True
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError:
                    if not saw_event:
                        self._raise_no_events(session_id, directory)
                    timed_out = True
                    break

                if item is _STREAM_END:
                    break
                if isinstance(item, _StreamFailure):
                    last_error = item.error
                    continue
                if isinstance(item, _PermissionEvent):
                    saw_event = True
                    request = item.request
                    if request.id in handled_permissions:
                        continue
                    handled_permissions.add(request.id)
                    await self._dispatch_permission(request, pending_permissions)
                    continue

                event = cast(dict[str, Any], item)
                event_id = _optional_str(event.get("id"))
                if _is_duplicate(event_id, seen_ids, seen_order):
                    continue

                saw_event = True
                deadline = loop.time() + effective_timeout
                idle = await self._process_event(
                    event,
                    session_id=session_id,
                    parts=parts,
                    handled_permissions=handled_permissions,
                    pending_permissions=pending_permissions,
                )
                if idle:
                    break

            # Give an in-flight reply a chance to leave before we return.
            await self._finish_permissions(
                pending_permissions, cancelled=self._cancelled or timed_out
            )

            if self._cancelled:
                return AIBackendResult(session_id=session_id, cancelled=True)

            text = "".join(parts.values())
            if timed_out:
                logger.warning(
                    "OpenCode prompt idle timeout",
                    extra={
                        "event": "opencode_prompt_timeout",
                        "session_id": session_id,
                        "timeout_s": effective_timeout,
                    },
                )
                await self._abort()
                return AIBackendResult(
                    stdout=text,
                    stderr=f"Timeout: el prompt estuvo {effective_timeout}s sin actividad.",
                    returncode=-1,
                    timed_out=True,
                    session_id=session_id,
                )

            if not text:
                text = await self._fallback_text(session_id, directory)

            if not text and last_error is not None:
                return AIBackendResult(
                    stderr=str(last_error),
                    returncode=1,
                    session_id=session_id,
                )

            return AIBackendResult(stdout=text, session_id=session_id)
        finally:
            self._queue = None
            self._pump_task = None
            stop.set()
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump

    def _raise_no_events(self, session_id: str, directory: str | None) -> NoReturn:
        """Fail fast when the SSE stream produced no event for this session."""
        resolved = directory or self._client.directory
        logger.error(
            "OpenCode stream produced no events after the prompt",
            extra={
                "event": "opencode_stream_no_events",
                "session_id": session_id,
                "directory": resolved,
                "timeout_s": self._first_event_timeout,
            },
        )
        raise OpenCodeStreamError(
            f"El stream SSE no emitió eventos para la sesión {session_id} en "
            f"{self._first_event_timeout:g}s (directory={resolved}). Revisa que el "
            "workdir de la conversación sea el directorio del proyecto.",
            directory=directory,
        )

    async def _process_event(
        self,
        event: dict[str, Any],
        *,
        session_id: str,
        parts: dict[str, str],
        handled_permissions: set[str],
        pending_permissions: set[asyncio.Task[None]],
    ) -> bool:
        """Apply one SSE event. Returns True when the turn is idle (finished)."""
        event_type = event.get("type")
        properties = event.get("properties")
        props = cast(dict[str, Any], properties) if isinstance(properties, dict) else {}

        if not _belongs_to_session(props, session_id):
            return False

        if event_type == "message.part.delta":
            field = props.get("field")
            if isinstance(field, str) and field != "text":
                return False
            delta = props.get("delta")
            if isinstance(delta, str) and delta:
                part_id = _optional_str(props.get("partID")) or _optional_str(props.get("partId"))
                key = part_id or "text"
                parts[key] = parts.get(key, "") + delta
                if self._progress is not None:
                    await self._progress.emit("".join(parts.values()))
            return False

        if event_type == "message.part.updated":
            part = props.get("part")
            if not isinstance(part, dict):
                return False
            part_data = cast(dict[str, Any], part)
            part_session = part_data.get("sessionID")
            if isinstance(part_session, str) and part_session and part_session != session_id:
                return False
            if part_data.get("type") != "text":
                return False
            text = part_data.get("text")
            if not isinstance(text, str):
                return False
            key = _optional_str(part_data.get("id")) or "text"
            parts[key] = text
            if self._progress is not None:
                await self._progress.emit("".join(parts.values()))
            return False

        if event_type == "permission.asked":
            request = _parse_permission_event(props, session_id)
            if not request.id or request.id in handled_permissions:
                return False
            handled_permissions.add(request.id)
            await self._dispatch_permission(request, pending_permissions)
            return False

        return event_type == "session.idle"

    async def _dispatch_permission(
        self,
        request: PermissionRequest,
        pending: set[asyncio.Task[None]],
    ) -> None:
        """Reply now (fail closed) or delegate to the Phase 3 handler."""
        if self._permission_handler is None:
            logger.warning(
                "Permission request rejected: approver not configured",
                extra={
                    "event": "permission_fail_closed",
                    "request_id": request.id,
                    "session_id": request.session_id,
                },
            )
            await self._reply_permission(
                request,
                PermissionDecision(reply="reject", message=PHASE3_REJECT_MESSAGE),
            )
            return

        task = asyncio.create_task(self._resolve_permission(request))
        pending.add(task)
        task.add_done_callback(pending.discard)

    async def _resolve_permission(self, request: PermissionRequest) -> None:
        handler = self._permission_handler
        if handler is None:  # defensive: the handler can never disappear here
            return
        try:
            decision = await handler(request)
        except Exception:
            log_exception(
                "permission_handler_failed",
                module=__name__,
                level=logging.ERROR,
            )
            decision = PermissionDecision(
                reply="reject",
                message="Error interno al solicitar la aprobación.",
            )
        await self._reply_permission(request, decision)

    async def _reply_permission(
        self,
        request: PermissionRequest,
        decision: PermissionDecision,
    ) -> None:
        try:
            await self._client.reply_permission(
                request.id,
                decision.reply,
                decision.message,
                directory=self._active_directory,
            )
        except Exception:
            log_exception(
                "permission_reply_failed",
                module=__name__,
                level=logging.ERROR,
            )

    @staticmethod
    async def _finish_permissions(
        pending: set[asyncio.Task[None]],
        *,
        cancelled: bool,
    ) -> None:
        if not pending:
            return
        if cancelled:
            for task in pending:
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        pending.clear()

    # ── SSE pump: reconnect, backoff, reconcile ────────────────────────────

    async def _pump(
        self,
        session_id: str,
        queue: asyncio.Queue[object],
        connected: asyncio.Event,
        stop: asyncio.Event,
        directory: str | None,
    ) -> None:
        """Feed events into ``queue`` forever; reconnect forever while alive."""
        attempt = 0
        while not stop.is_set():
            if attempt:
                # We may have missed a permission while disconnected.
                for request in await self._pending_permissions(session_id, directory):
                    await queue.put(_PermissionEvent(request))
                delay = min(
                    self._reconnect_initial * (2 ** (attempt - 1)),
                    self._reconnect_max,
                )
                logger.warning(
                    "Reconnecting to the OpenCode SSE stream",
                    extra={
                        "event": "opencode_sse_reconnect",
                        "session_id": session_id,
                        "attempt": attempt,
                        "delay_s": round(delay, 3),
                    },
                )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                    break
                except TimeoutError:
                    pass

            attempt += 1
            try:
                async for event in self._client.stream_events(
                    on_connect=connected.set,
                    directory=directory,
                ):
                    if stop.is_set():
                        break
                    await queue.put(event)
                if stop.is_set():
                    break
                await queue.put(
                    _StreamFailure(
                        OpenCodeUnavailableError("La conexión SSE terminó inesperadamente.")
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await queue.put(_StreamFailure(exc))

        await queue.put(_STREAM_END)

    async def _pending_permissions(
        self,
        session_id: str,
        directory: str | None,
    ) -> list[PermissionRequest]:
        try:
            pending = await self._client.list_permissions(directory=directory)
        except Exception:
            log_exception(
                "opencode_permission_reconcile_failed",
                module=__name__,
                level=logging.WARNING,
            )
            return []
        return [
            request
            for request in pending
            if not request.session_id or request.session_id == session_id
        ]

    # ── Fallback ───────────────────────────────────────────────────────────

    async def _fallback_text(self, session_id: str, directory: str | None) -> str:
        """Last-resort final text: the last assistant message from the server."""
        try:
            messages = await self._client.get_messages(session_id, directory=directory)
        except OpenCodeServerError:
            log_exception(
                "opencode_messages_fallback_failed",
                module=__name__,
                level=logging.WARNING,
            )
            return ""
        return _assistant_text(messages)


# ── Event parsing helpers ──────────────────────────────────────────────────


def _belongs_to_session(props: dict[str, Any], session_id: str) -> bool:
    event_session = props.get("sessionID")
    return not isinstance(event_session, str) or not event_session or event_session == session_id


def _is_duplicate(
    event_id: str | None,
    seen: set[str],
    order: deque[str],
) -> bool:
    if not event_id:
        return False
    if event_id in seen:
        return True
    seen.add(event_id)
    order.append(event_id)
    if len(order) > DEDUP_LIMIT:
        seen.discard(order.popleft())
    return False


def _parse_permission_event(props: dict[str, Any], session_id: str) -> PermissionRequest:
    tool = props.get("tool")
    tool_data = cast(dict[str, Any], tool) if isinstance(tool, dict) else {}
    metadata = props.get("metadata")
    metadata_data = cast(dict[str, Any], metadata) if isinstance(metadata, dict) else {}
    return PermissionRequest(
        id=_optional_str(props.get("id")) or "",
        session_id=_optional_str(props.get("sessionID")) or session_id,
        permission=_optional_str(props.get("permission")) or "",
        patterns=_str_tuple(props.get("patterns")),
        metadata=metadata_data,
        always=_str_tuple(props.get("always")),
        message_id=_optional_str(tool_data.get("messageID")),
        call_id=_optional_str(tool_data.get("callID")),
    )


def _assistant_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        info = message.get("info")
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        texts = [
            part["text"]
            for part in parts
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ]
        if texts:
            return "\n".join(texts)
    return ""


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))
