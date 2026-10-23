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
from dataclasses import dataclass, field
from typing import Any, NoReturn, cast

from services.ai_backend import AIBackendResult
from services.opencode_server.client import OpenCodeServerClient
from services.opencode_server.errors import (
    OpenCodeServerError,
    OpenCodeStreamError,
    OpenCodeUnavailableError,
)
from services.opencode_server.manager import OpenCodeServerManager
from services.permissions import (
    PERMISSION_BUDGET_EXPIRED_REASON,
    PermissionDecision,
    PermissionRequest,
)
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
DEFAULT_PERMISSION_TIMEOUT = 300.0
#: Slack on top of the router timeout so its reject reply can settle the task
#: before the backend would cancel it (defense in depth only).
PERMISSION_GRACE = 1.0
PHASE3_REJECT_MESSAGE = "Las aprobaciones se habilitan en la Fase 3."

#: ``info.finish`` values that mean the assistant turn is over. ``tool-calls``
#: is explicitly NOT final: the model will continue after the tool result.
_FINAL_FINISHES = frozenset(
    {"stop", "length", "content-filter", "content_filter", "end_turn", "end-turn"}
)

_STREAM_END = object()
_PERMISSION_SETTLED = object()


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


@dataclass
class _TurnState:
    """Per-turn accumulation split by part kind and message role.

    Evidence from a live 1.19.1 SSE capture: ``message.part.delta`` events carry
    ``field="text"`` for REASONING parts too, and the user's own prompt is
    emitted as a ``text`` part. Accumulating every ``text`` part therefore
    produced a final answer that started with the prompt and the whole chain of
    thought (the reported "frozen" wrong response). Only assistant ``text``
    parts are user-visible; reasoning is kept apart for live progress.
    """

    text_parts: dict[str, str] = field(default_factory=dict)
    text_order: list[str] = field(default_factory=list)
    reasoning_parts: dict[str, str] = field(default_factory=dict)
    reasoning_order: list[str] = field(default_factory=list)
    part_types: dict[str, str] = field(default_factory=dict)
    part_messages: dict[str, str] = field(default_factory=dict)
    message_roles: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """The final user-visible response (assistant text parts only)."""
        return "".join(self.text_parts.get(part_id, "") for part_id in self.text_order)

    @property
    def reasoning(self) -> str:
        return "".join(self.reasoning_parts.get(part_id, "") for part_id in self.reasoning_order)

    @property
    def progress(self) -> str:
        """Live placeholder: visible text once it exists, reasoning before it."""
        return self.text or self.reasoning

    def set_part_type(self, part_id: str, part_type: str) -> None:
        self.part_types[part_id] = part_type

    def set_part_message(self, part_id: str, message_id: str) -> None:
        self.part_messages[part_id] = message_id

    def is_user_part(self, part_id: str) -> bool:
        message_id = self.part_messages.get(part_id)
        return message_id is not None and self.message_roles.get(message_id) == "user"

    def set_text(self, part_id: str, text: str) -> None:
        if part_id not in self.text_parts:
            self.text_order.append(part_id)
        self.text_parts[part_id] = text

    def append_text(self, part_id: str, delta: str) -> None:
        if part_id not in self.text_parts:
            self.text_order.append(part_id)
        self.text_parts[part_id] = self.text_parts.get(part_id, "") + delta

    def set_reasoning(self, part_id: str, text: str) -> None:
        if part_id not in self.reasoning_parts:
            self.reasoning_order.append(part_id)
        self.reasoning_parts[part_id] = text

    def append_reasoning(self, part_id: str, delta: str) -> None:
        if part_id not in self.reasoning_parts:
            self.reasoning_order.append(part_id)
        self.reasoning_parts[part_id] = self.reasoning_parts.get(part_id, "") + delta


class _PermissionBudget:
    """Absolute deadline for one spell with at least one pending permission.

    The deadline is armed ONCE when the first request of the spell appears and
    is NOT re-armed when a permission settles while others are still pending:
    "wait up to ``permission_timeout + grace``" describes the whole human
    interaction, not each individual wait. The spell ends (``clear()``) when
    every request has settled; a later spell gets a fresh budget.

    ``timeout <= 0`` means no deadline at all (D4: wait indefinitely).
    """

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._deadline: float | None = None

    def remaining(self, now: float) -> float | None:
        if self._timeout <= 0:
            return None
        if self._deadline is None:
            self._deadline = now + self._timeout + PERMISSION_GRACE
        return self._deadline - now

    def clear(self) -> None:
        self._deadline = None


class OpenCodeServerBackend:
    """Streams a prompt through ``opencode serve`` and returns the final text."""

    #: ``execute`` pushes incremental text through ``on_progress``.
    supports_live_progress = True

    def __init__(
        self,
        client: OpenCodeServerClient,
        *,
        permission_handler: PermissionHandler | None = None,
        timeout: float = 300.0,
        permission_timeout: float = DEFAULT_PERMISSION_TIMEOUT,
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
        self._permission_timeout = permission_timeout
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
        timeout: float | None = None,
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
        effective_timeout: float,
        directory: str | None,
    ) -> AIBackendResult:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[object] = asyncio.Queue()
        connected = asyncio.Event()
        stop = asyncio.Event()
        self._queue = queue

        pump = asyncio.create_task(self._pump(session_id, queue, connected, stop, directory))
        self._pump_task = pump

        turn = _TurnState()
        seen_ids: set[str] = set()
        seen_order: deque[str] = deque()
        handled_permissions: set[str] = set()
        pending_permissions: set[asyncio.Task[None]] = set()
        last_error: Exception | None = None
        timed_out = False
        terminal_event: str | None = None

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
            waited_for_permission = False
            permission_budget = _PermissionBudget(self._permission_timeout)
            permission_expired = False
            while not self._cancelled:
                if pending_permissions:
                    # A human is deciding: SUSPEND the prompt idle deadline
                    # entirely. The turn is bounded only by the permission
                    # budget (0 = wait indefinitely for the human), never by
                    # OPENCODE_TIMEOUT. The budget is absolute for the whole
                    # spell, so settling one of several dialogs does not
                    # restart it.
                    waited_for_permission = True
                    wait_timeout = permission_budget.remaining(loop.time())
                    if wait_timeout is not None and wait_timeout <= 0:
                        timed_out = True
                        permission_expired = True
                        break
                else:
                    if waited_for_permission:
                        # The dialog closed: restart the idle deadline so the
                        # human wait does not consume the prompt budget.
                        deadline = loop.time() + effective_timeout
                        waited_for_permission = False
                    permission_budget.clear()
                    remaining = deadline - loop.time()
                    if not saw_event:
                        # A healthy stream emits session/message events
                        # immediately. No event at all means the stream is
                        # scoped wrong or dead: fail fast instead of waiting
                        # out the (possibly 20 min) idle deadline in silence.
                        remaining = min(remaining, first_deadline - loop.time())
                    if remaining <= 0:
                        if not saw_event:
                            self._raise_no_events(session_id, directory)
                        timed_out = True
                        break
                    wait_timeout = remaining
                try:
                    if pending_permissions:
                        item = await self._wait_with_permission(
                            queue, pending_permissions, wait_timeout
                        )
                        if item is _PERMISSION_SETTLED:
                            continue
                    else:
                        item = await asyncio.wait_for(queue.get(), timeout=wait_timeout)
                except TimeoutError:
                    if not saw_event:
                        self._raise_no_events(session_id, directory)
                    if pending_permissions:
                        permission_expired = True
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
                terminal = await self._process_event(
                    event,
                    session_id=session_id,
                    turn=turn,
                    handled_permissions=handled_permissions,
                    pending_permissions=pending_permissions,
                )
                if terminal is not None:
                    terminal_event = terminal
                    break

            # Give an in-flight reply a chance to leave before we return.
            await self._finish_permissions(
                pending_permissions,
                cancelled=self._cancelled or timed_out,
                expired=permission_expired,
            )

            if self._cancelled:
                return AIBackendResult(session_id=session_id, cancelled=True)

            if timed_out:
                await self._abort()
                timeout_s = self._permission_timeout if permission_expired else effective_timeout
                logger.error(
                    "OpenCode permission budget expired without a decision"
                    if permission_expired
                    else "OpenCode prompt timed out without a terminal event",
                    extra={
                        "event": "opencode_prompt_timeout",
                        "session_id": session_id,
                        "timeout_s": timeout_s,
                        "reason": ("permission_budget_expired" if permission_expired else "idle"),
                        "text_length": len(turn.text),
                        "reasoning_length": len(turn.reasoning),
                    },
                )
                if permission_expired:
                    raise OpenCodeStreamError(
                        f"El permiso de OpenCode no se resolvió en {timeout_s:g}s "
                        "(la interfaz no respondió a la solicitud).",
                        directory=directory,
                    )
                raise OpenCodeStreamError(
                    f"El turno de OpenCode no terminó en {effective_timeout:g}s "
                    "(sin session.idle ni mensaje final).",
                    directory=directory,
                )

            logger.info(
                "OpenCode stream completed",
                extra={
                    "event": "opencode_stream_completed",
                    "session_id": session_id,
                    "terminal_event": terminal_event,
                    "text_length": len(turn.text),
                    "reasoning_length": len(turn.reasoning),
                },
            )

            text = turn.text
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

    async def _wait_with_permission(
        self,
        queue: asyncio.Queue[object],
        pending: set[asyncio.Task[None]],
        timeout: float | None,
    ) -> object:
        """Await the next SSE item OR any pending permission settling.

        Returns the queue item when the stream wins, or ``_PERMISSION_SETTLED``
        when a permission task finished first (the caller re-checks the pending
        set and keeps waiting for the rest). Raises ``TimeoutError`` ONLY when
        the wait itself timed out with nothing completed: a settled permission
        next to another still-pending one is not an expired budget.
        """
        getter: asyncio.Task[object] = asyncio.create_task(queue.get())
        waiters: set[asyncio.Future[Any]] = {getter, *pending}
        try:
            done, _ = await asyncio.wait(
                waiters,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            getter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await getter
            raise
        if not done:
            # Nothing completed before the timeout: a real budget expiry.
            getter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await getter
            raise TimeoutError
        getter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await getter
        for finished in done:
            if finished is not getter:
                pending.discard(cast(asyncio.Task[None], finished))
        if getter.done() and not getter.cancelled():
            # The item arrived in the same tick we cancelled the getter: never
            # drop an SSE event just because a permission settled first.
            return getter.result()
        return _PERMISSION_SETTLED

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
        turn: _TurnState,
        handled_permissions: set[str],
        pending_permissions: set[asyncio.Task[None]],
    ) -> str | None:
        """Apply one SSE event. Returns a terminal reason when the turn ends."""
        event_type = event.get("type")
        properties = event.get("properties")
        props = cast(dict[str, Any], properties) if isinstance(properties, dict) else {}

        if not _belongs_to_session(props, session_id):
            return None

        if event_type == "message.updated":
            return self._process_message_updated(props, turn)

        if event_type == "message.part.delta":
            await self._process_delta(props, turn)
            return None

        if event_type == "message.part.updated":
            await self._process_part_updated(props, session_id, turn)
            return None

        if event_type == "permission.asked":
            request = _parse_permission_event(props, session_id)
            if not request.id or request.id in handled_permissions:
                return None
            handled_permissions.add(request.id)
            await self._dispatch_permission(request, pending_permissions)
            return None

        if event_type == "session.status":
            status = props.get("status")
            if isinstance(status, dict):
                status_type = status.get("type")
                if status_type == "idle":
                    return "session.status.idle"
                if status_type == "error":
                    raise OpenCodeStreamError(
                        f"OpenCode reportó un error en la sesión ({_error_message(status)}).",
                        directory=self._active_directory,
                    )
            return None

        if event_type == "session.error":
            raise OpenCodeStreamError(
                f"OpenCode reportó un error en la sesión ({_error_message(props)}).",
                directory=self._active_directory,
            )

        if event_type == "session.idle":
            return "session.idle"
        return None

    def _process_message_updated(self, props: dict[str, Any], turn: _TurnState) -> str | None:
        """Track message roles and use a completed final message as terminal.

        ``session.idle`` was missing in the reported freeze (a ``task`` tool
        subagent never started, so the parent stayed ``busy`` forever). A
        completed assistant message whose ``finish`` reason is final is a
        deterministic equivalent: OpenCode only emits it when the step has no
        more tool calls to run.
        """
        info = props.get("info")
        if not isinstance(info, dict):
            return None
        info_data = cast(dict[str, Any], info)
        message_id = _optional_str(info_data.get("id"))
        role = _optional_str(info_data.get("role"))
        if message_id and role:
            turn.message_roles[message_id] = role

        if info_data.get("error") is not None and info_data.get("error") != {}:
            raise OpenCodeStreamError(
                f"OpenCode reportó un error en el mensaje ({_error_message(info_data)}).",
                directory=self._active_directory,
            )

        if role != "assistant" or not _is_final_assistant_message(info_data):
            return None
        return "message.updated.completed"

    async def _process_delta(self, props: dict[str, Any], turn: _TurnState) -> None:
        """Append a text/reasoning delta; never mix them in the final text."""
        field = props.get("field")
        if isinstance(field, str) and field != "text":
            return
        delta = props.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        part_id = _optional_str(props.get("partID")) or _optional_str(props.get("partId"))
        if not part_id:
            return
        part_type = turn.part_types.get(part_id)
        if part_type == "reasoning":
            turn.append_reasoning(part_id, delta)
        elif part_type in (None, "text") and not turn.is_user_part(part_id):
            # ``None`` keeps compatibility with streams that omit the prior
            # ``message.part.updated``; a known user part is always excluded.
            turn.append_text(part_id, delta)
        else:
            return
        await self._emit_progress(turn)

    async def _process_part_updated(
        self,
        props: dict[str, Any],
        session_id: str,
        turn: _TurnState,
    ) -> None:
        part = props.get("part")
        if not isinstance(part, dict):
            return
        part_data = cast(dict[str, Any], part)
        part_session = part_data.get("sessionID")
        if isinstance(part_session, str) and part_session and part_session != session_id:
            return
        part_id = _optional_str(part_data.get("id"))
        part_type = _optional_str(part_data.get("type"))
        if not part_id or not part_type:
            return
        turn.set_part_type(part_id, part_type)
        message_id = _optional_str(part_data.get("messageID"))
        if message_id:
            turn.set_part_message(part_id, message_id)

        text = part_data.get("text")
        if not isinstance(text, str):
            return
        if part_type == "text":
            if turn.is_user_part(part_id):
                return
            turn.set_text(part_id, text)
        elif part_type == "reasoning":
            turn.set_reasoning(part_id, text)
        else:
            return
        await self._emit_progress(turn)

    async def _emit_progress(self, turn: _TurnState) -> None:
        if self._progress is not None:
            await self._progress.emit(turn.progress)

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
        expired: bool = False,
    ) -> None:
        """Settle in-flight permission tasks before the turn returns.

        ``expired`` tags the cancellation so the router reports "the human
        timed out" instead of "the turn was interrupted" (both are
        cancellations; only the notice differs).
        """
        if not pending:
            return
        if cancelled:
            reason = PERMISSION_BUDGET_EXPIRED_REASON if expired else None
            for task in pending:
                task.cancel(reason)
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


def _is_final_assistant_message(info: dict[str, Any]) -> bool:
    """True when an assistant message is completed AND the turn is over."""
    if info.get("role") != "assistant":
        return False
    time_data = info.get("time")
    if not isinstance(time_data, dict) or time_data.get("completed") is None:
        return False
    finish = info.get("finish")
    return isinstance(finish, str) and finish in _FINAL_FINISHES


def _error_message(payload: dict[str, Any]) -> str:
    """Best-effort human-readable message from ``session.error``/``info.error``."""
    error = payload.get("error", payload)
    if isinstance(error, dict):
        data = error.get("data")
        if isinstance(data, dict):
            data_message = data.get("message")
            if isinstance(data_message, str) and data_message:
                return data_message
        for key in ("message", "name", "type"):
            value = error.get(key)
            if isinstance(value, str) and value:
                return value
    if isinstance(error, str) and error:
        return error
    return "error desconocido"


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
