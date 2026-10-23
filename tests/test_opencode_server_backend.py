"""Tests for ``OpenCodeServerBackend`` — no live server.

A fake client plays a scripted SSE sequence so every edge of the streaming
contract is deterministic: stream-before-prompt ordering, text accumulation,
progress throttling, idle termination, the ``get_messages`` fallback, the
idle-reset timeout, cancellation, the fail-closed permission stub, event-id
deduplication, and reconnection with permission reconciliation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from services.opencode_server.errors import OpenCodeStreamError, OpenCodeUnavailableError
from services.opencode_server_backend import (
    PHASE3_REJECT_MESSAGE,
    OpenCodeServerBackend,
    _PermissionBudget,
)
from services.permission_router import TIMEOUT_MESSAGE, PermissionRouter
from services.permissions import PermissionDecision, PermissionRequest

SID = "ses_backend_test"
OTHER_SID = "ses_other"


def _event(event_type: str, properties: dict[str, Any], event_id: str = "") -> dict[str, Any]:
    return {"id": event_id, "type": event_type, "properties": properties}


def _delta(text: str, *, event_id: str = "", part_id: str = "p1") -> dict[str, Any]:
    return _event(
        "message.part.delta",
        {"sessionID": SID, "partID": part_id, "field": "text", "delta": text},
        event_id,
    )


def _updated(text: str, *, event_id: str = "", part_id: str = "p1") -> dict[str, Any]:
    return _event(
        "message.part.updated",
        {"sessionID": SID, "part": {"id": part_id, "type": "text", "text": text}},
        event_id,
    )


def _idle(event_id: str = "") -> dict[str, Any]:
    return _event("session.idle", {"sessionID": SID}, event_id)


def _status(status_type: str) -> dict[str, Any]:
    return _event("session.status", {"sessionID": SID, "status": {"type": status_type}})


def _message_updated(
    message_id: str,
    role: str,
    *,
    completed: bool = False,
    finish: str | None = None,
    event_id: str = "",
) -> dict[str, Any]:
    info: dict[str, Any] = {"id": message_id, "role": role, "sessionID": SID}
    if completed:
        info["time"] = {"created": 1, "completed": 2}
    if finish is not None:
        info["finish"] = finish
    return _event("message.updated", {"sessionID": SID, "info": info}, event_id)


def _part(
    part_id: str,
    part_type: str,
    text: str = "",
    *,
    message_id: str | None = None,
    event_id: str = "",
) -> dict[str, Any]:
    part: dict[str, Any] = {
        "id": part_id,
        "type": part_type,
        "text": text,
        "sessionID": SID,
    }
    if message_id is not None:
        part["messageID"] = message_id
    return _event("message.part.updated", {"sessionID": SID, "part": part}, event_id)


def _reasoning_delta(text: str, *, part_id: str = "p_r", event_id: str = "") -> dict[str, Any]:
    """Live 1.19.1 sends ``field="text"`` for reasoning deltas too."""
    return _event(
        "message.part.delta",
        {"sessionID": SID, "partID": part_id, "field": "text", "delta": text},
        event_id,
    )


def _permission(
    request_id: str = "per_1",
    *,
    session_id: str = SID,
    event_id: str = "",
) -> dict[str, Any]:
    return _event(
        "permission.asked",
        {
            "id": request_id,
            "sessionID": session_id,
            "permission": "bash",
            "patterns": ["git commit *"],
            "metadata": {"command": "git commit -m x"},
            "always": ["git commit *"],
            "tool": {"messageID": "msg_1", "callID": "call_1"},
        },
        event_id,
    )


class _Hang:
    """Script marker: block the stream until the fake is released."""


class FakeClient:
    """Minimal scripted ``OpenCodeServerClient`` double."""

    def __init__(
        self,
        *,
        session_id: str = SID,
        streams: list[Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
        permissions: list[PermissionRequest] | None = None,
    ) -> None:
        self.session_id = session_id
        self.streams = list(streams or [])
        self.messages = messages or []
        self.permissions = permissions or []
        self.order: list[str] = []
        self.created_titles: list[str] = []
        self.prompts: list[tuple[str, str, str | None]] = []
        self.replies: list[tuple[str, str, str | None]] = []
        self.message_requests: list[str] = []
        self.permission_list_calls = 0
        self.aborted: list[str] = []
        self.stream_count = 0
        self.directory = "C:/fake"
        self.directories: dict[str, list[str | None]] = {
            "create": [],
            "stream": [],
            "prompt": [],
            "messages": [],
            "permissions": [],
            "reply": [],
            "abort": [],
        }
        self._release = asyncio.Event()

    def release(self) -> None:
        self._release.set()

    async def create_session(self, title: str, *, directory: str | None = None) -> str:
        self.order.append("create")
        self.created_titles.append(title)
        self.directories["create"].append(directory)
        return self.session_id

    def stream_events(
        self,
        *,
        on_connect: Any = None,
        directory: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        self.directories["stream"].append(directory)
        return self._stream(on_connect)

    async def _stream(self, on_connect: Any) -> AsyncIterator[dict[str, Any]]:
        self.order.append("stream")
        self.stream_count += 1
        script = self.streams.pop(0) if self.streams else []
        if on_connect is not None:
            on_connect()
        if isinstance(script, Exception):
            raise script
        for item in script:
            if isinstance(item, _Hang):
                await self._release.wait()
            elif isinstance(item, float):
                await asyncio.sleep(item)
            else:
                yield item

    async def send_prompt(
        self,
        session_id: str,
        prompt: str,
        *,
        model: str,
        agent: str | None = None,
        directory: str | None = None,
    ) -> None:
        self.order.append("prompt")
        self.prompts.append((session_id, prompt, agent))
        self.directories["prompt"].append(directory)

    async def get_messages(
        self,
        session_id: str,
        limit: int = 50,
        *,
        directory: str | None = None,
    ) -> list[dict[str, Any]]:
        self.message_requests.append(session_id)
        self.directories["messages"].append(directory)
        return self.messages

    async def list_permissions(
        self,
        *,
        directory: str | None = None,
    ) -> list[PermissionRequest]:
        self.permission_list_calls += 1
        self.directories["permissions"].append(directory)
        return self.permissions

    async def reply_permission(
        self,
        request_id: str,
        reply: str,
        message: str | None = None,
        *,
        directory: str | None = None,
    ) -> bool:
        self.replies.append((request_id, reply, message))
        self.directories["reply"].append(directory)
        return True

    async def abort(self, session_id: str, *, directory: str | None = None) -> bool:
        self.aborted.append(session_id)
        self.directories["abort"].append(directory)
        return True

    async def close(self) -> None:
        return None


def _backend(client: FakeClient, **kwargs: Any) -> OpenCodeServerBackend:
    kwargs.setdefault("progress_interval", 0.0)
    return OpenCodeServerBackend(client, **kwargs)  # type: ignore[arg-type]


# ── Session + ordering ────────────────────────────────────────────────────


async def test_stream_is_opened_before_the_prompt() -> None:
    client = FakeClient(streams=[[_delta("Hola"), _idle()]])

    result = await _backend(client).execute(prompt="hola", model="openai/gpt", session_id=None)

    assert client.order[:3] == ["create", "stream", "prompt"]
    assert result.stdout == "Hola"
    assert result.session_id == SID
    assert client.created_titles == ["hola"]


async def test_explicit_session_id_is_used_without_creating_one() -> None:
    client = FakeClient(streams=[[_delta("ok"), _idle()]])

    result = await _backend(client).execute(
        prompt="hola",
        model="openai/gpt",
        session_id=SID,
    )

    assert "create" not in client.order
    assert result.session_id == SID
    assert client.prompts[0][0] == SID


async def test_manager_ensure_running_is_lazy_and_runs_once() -> None:
    class FakeManager:
        def __init__(self) -> None:
            self.calls = 0
            self.directories: list[str | None] = []

        async def ensure_running(self, *, directory: str | None = None) -> None:
            self.calls += 1
            self.directories.append(directory)

    manager = FakeManager()
    client = FakeClient(streams=[[_delta("a"), _idle()], [_delta("b"), _idle()]])
    backend = _backend(client, manager=manager)

    assert manager.calls == 0
    await backend.execute(
        prompt="a", model="openai/gpt", session_id=None, workdir="C:/conv/project"
    )
    await backend.execute(prompt="b", model="openai/gpt", session_id=None)

    assert manager.calls == 1
    assert manager.directories == ["C:/conv/project"]


# ── Text accumulation + progress ──────────────────────────────────────────


async def test_deltas_append_and_updates_replace_by_part() -> None:
    client = FakeClient(
        streams=[
            [
                _delta("Hola "),
                _delta("mun"),
                _updated("Hola mundo!"),
                _idle(),
            ]
        ]
    )

    result = await _backend(client).execute(prompt="x", model="openai/gpt", session_id=None)

    assert result.stdout == "Hola mundo!"


async def test_on_progress_receives_throttled_tail() -> None:
    seen: list[str] = []

    async def progress(text: str) -> None:
        seen.append(text)

    client = FakeClient(streams=[[_delta("123456"), _delta("7890"), _delta("abc"), _idle()]])

    result = await _backend(client, progress_tail=5).execute(
        prompt="x",
        model="openai/gpt",
        session_id=None,
        on_progress=progress,
    )

    assert result.stdout == "1234567890abc"
    assert seen == ["23456", "67890", "90abc"]
    assert all(len(text) <= 5 for text in seen)


async def test_idle_stops_consuming_the_stream() -> None:
    client = FakeClient(streams=[[_delta("antes"), _idle(), _delta("despues")]])

    result = await _backend(client).execute(prompt="x", model="openai/gpt", session_id=None)

    assert result.stdout == "antes"


async def test_prompt_and_reasoning_are_excluded_from_the_final_text() -> None:
    """Regression: the user prompt and chain of thought leaked into stdout.

    Live evidence: ``message.part.delta`` uses ``field="text"`` for reasoning
    parts, and the user's prompt is a ``text`` part too. Only assistant text
    parts may reach ``AIBackendResult.stdout``.
    """
    client = FakeClient(
        streams=[
            [
                _message_updated("msg_u", "user"),
                _part("p_u", "text", "haz un commit", message_id="msg_u"),
                _message_updated("msg_a", "assistant"),
                _part("p_r", "reasoning", ""),
                _reasoning_delta("pensando paso a paso...", part_id="p_r"),
                _part("p_r", "reasoning", "pensando paso a paso..."),
                _part("p_t", "text", ""),
                _delta("Listo", part_id="p_t"),
                _part("p_t", "text", "Listo", message_id="msg_a"),
                _idle(),
            ]
        ]
    )

    result = await _backend(client).execute(prompt="x", model="openai/gpt", session_id=None)

    assert result.stdout == "Listo"


async def test_progress_uses_reasoning_then_text_but_final_is_text_only() -> None:
    seen: list[str] = []

    async def progress(text: str) -> None:
        seen.append(text)

    client = FakeClient(
        streams=[
            [
                _message_updated("msg_a", "assistant"),
                _part("p_r", "reasoning", ""),
                _reasoning_delta("pensando", part_id="p_r"),
                _part("p_t", "text", ""),
                _delta("Respuesta", part_id="p_t"),
                _part("p_t", "text", "Respuesta", message_id="msg_a"),
                _idle(),
            ]
        ]
    )

    result = await _backend(client).execute(
        prompt="x",
        model="openai/gpt",
        session_id=None,
        on_progress=progress,
    )

    assert seen[0] == "pensando"
    assert seen[-1] == "Respuesta"
    assert result.stdout == "Respuesta"
    assert "pensando" not in result.stdout


async def test_session_status_idle_terminates_without_session_idle() -> None:
    client = FakeClient(streams=[[_delta("Hola"), _status("idle")]])

    result = await _backend(client).execute(prompt="x", model="openai/gpt", session_id=None)

    assert result.stdout == "Hola"


async def test_completed_assistant_message_terminates_without_session_idle() -> None:
    """Missing ``session.idle`` must not hang: a final completed message ends it."""
    client = FakeClient(
        streams=[
            [
                _message_updated("msg_a", "assistant"),
                _delta("Hola", part_id="p1"),
                _part("p1", "text", "Hola", message_id="msg_a"),
                _message_updated("msg_a", "assistant", completed=True, finish="stop"),
            ]
        ]
    )

    result = await _backend(client).execute(prompt="x", model="openai/gpt", session_id=None)

    assert result.stdout == "Hola"


async def test_tool_calls_finish_is_not_terminal() -> None:
    client = FakeClient(
        streams=[
            [
                _message_updated("msg_a", "assistant"),
                _delta("Paso 1", part_id="p1"),
                _message_updated("msg_a", "assistant", completed=True, finish="tool-calls"),
                _delta(" y 2", part_id="p2"),
                _part("p2", "text", " y 2", message_id="msg_a"),
                _idle(),
            ]
        ]
    )

    result = await _backend(client).execute(prompt="x", model="openai/gpt", session_id=None)

    assert result.stdout == "Paso 1 y 2"


async def test_session_error_raises_a_typed_error() -> None:
    client = FakeClient(
        streams=[
            [
                _message_updated("msg_a", "assistant"),
                _event(
                    "session.error",
                    {"sessionID": SID, "error": {"message": "proveedor caído"}},
                ),
            ]
        ]
    )

    with pytest.raises(OpenCodeStreamError, match="proveedor caído"):
        await _backend(client).execute(prompt="x", model="openai/gpt", session_id=None)


# ── Fallback ──────────────────────────────────────────────────────────────


async def test_fallback_reads_last_assistant_message() -> None:
    client = FakeClient(
        streams=[[_idle()]],
        messages=[
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "pregunta"}]},
            {
                "info": {"role": "assistant"},
                "parts": [
                    {"type": "reasoning", "text": "pensando"},
                    {"type": "text", "text": "Respuesta final"},
                ],
            },
        ],
    )

    result = await _backend(client).execute(prompt="x", model="openai/gpt", session_id=None)

    assert result.stdout == "Respuesta final"
    assert client.message_requests == [SID]


# ── Timeout semantics ─────────────────────────────────────────────────────


async def test_timeout_is_reset_by_every_event() -> None:
    client = FakeClient(streams=[[_delta("a"), 0.6, _delta("b"), 0.6, _idle()]])

    result = await _backend(client).execute(
        prompt="x",
        model="openai/gpt",
        session_id=None,
        timeout=1,
    )

    assert result.timed_out is False
    assert result.stdout == "ab"


async def test_idle_timeout_without_terminal_event_raises_and_aborts() -> None:
    """A silent turn must raise a typed error, never look frozen forever."""
    client = FakeClient(streams=[[_delta("a"), _Hang()]])

    with pytest.raises(OpenCodeStreamError, match="no terminó"):
        await _backend(client).execute(
            prompt="x",
            model="openai/gpt",
            session_id=None,
            timeout=1,
        )

    assert client.aborted == [SID]


# ── Cancel ────────────────────────────────────────────────────────────────


async def test_cancel_returns_cancelled_and_aborts() -> None:
    client = FakeClient(streams=[[_delta("a"), _Hang()]])
    backend = _backend(client)

    task = asyncio.create_task(backend.execute(prompt="x", model="openai/gpt", session_id=None))
    for _ in range(100):
        if client.stream_count:
            break
        await asyncio.sleep(0.01)
    backend.cancel()
    result = await asyncio.wait_for(task, timeout=2)

    assert result.cancelled is True
    assert client.aborted == [SID]


# ── Permissions ───────────────────────────────────────────────────────────


async def test_permission_without_handler_fails_closed(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeClient(streams=[[_permission(), _idle()]])

    with caplog.at_level(logging.WARNING):
        result = await _backend(client).execute(prompt="x", model="openai/gpt", session_id=None)

    assert result.stdout == ""
    assert client.replies == [("per_1", "reject", PHASE3_REJECT_MESSAGE)]
    assert any(
        getattr(record, "event", "") == "permission_fail_closed" for record in caplog.records
    )


async def test_permission_handler_receives_request_and_replies() -> None:
    requests: list[PermissionRequest] = []

    async def handler(request: PermissionRequest) -> PermissionDecision:
        requests.append(request)
        return PermissionDecision(reply="once")

    client = FakeClient(streams=[[_permission(), _idle()]])

    await _backend(client, permission_handler=handler).execute(
        prompt="x",
        model="openai/gpt",
        session_id=None,
    )

    assert len(requests) == 1
    assert requests[0].permission == "bash"
    assert requests[0].patterns == ("git commit *",)
    assert client.replies == [("per_1", "once", None)]


async def test_permission_for_another_session_is_ignored() -> None:
    client = FakeClient(streams=[[_permission(session_id=OTHER_SID), _idle()]])

    await _backend(client).execute(prompt="x", model="openai/gpt", session_id=None)

    assert client.replies == []


async def test_permission_pauses_the_prompt_deadline() -> None:
    """A human wait must not consume ``OPENCODE_TIMEOUT`` (#2).

    The prompt timeout (0.15s) is shorter than the time the handler takes
    (0.2s). The old code cancelled the approval task at 0.15s: no reply was
    sent and the turn failed. The deadline is now suspended while pending.
    """

    async def handler(request: PermissionRequest) -> PermissionDecision:
        await asyncio.sleep(0.2)
        return PermissionDecision(reply="once")

    client = FakeClient(streams=[[_permission(), 0.3, _idle()]])

    result = await _backend(
        client,
        permission_handler=handler,
        timeout=0.15,
        permission_timeout=0.5,
    ).execute(prompt="x", model="openai/gpt", session_id=None)

    assert result.cancelled is False
    assert result.stdout == ""
    assert client.replies == [("per_1", "once", None)]
    assert client.aborted == []


async def test_zero_permission_timeout_never_auto_rejects() -> None:
    """``0`` means the human has no deadline inside the turn (#2 / D4)."""

    async def handler(request: PermissionRequest) -> PermissionDecision:
        await asyncio.sleep(0.2)
        return PermissionDecision(reply="once")

    client = FakeClient(streams=[[_permission(), 0.3, _idle()]])

    result = await _backend(
        client,
        permission_handler=handler,
        timeout=0.15,
        permission_timeout=0,
    ).execute(prompt="x", model="openai/gpt", session_id=None)

    assert result.cancelled is False
    assert client.replies == [("per_1", "once", None)]


async def test_permission_timeout_expiry_rejects_and_notifies_presenters() -> None:
    """The safety net still fires: reject the request and close the dialog."""
    resolved: list[tuple[str, PermissionDecision]] = []

    class _Presenter:
        async def show(self, request: PermissionRequest) -> None:
            return None

        async def resolve(self, request_id: str, decision: PermissionDecision) -> None:
            resolved.append((request_id, decision))

    router = PermissionRouter(presenters=[_Presenter()], timeout=0.1)
    client = FakeClient(streams=[[_permission(), 0.3, _idle()]])

    result = await _backend(
        client,
        permission_handler=router.request_permission,
        timeout=0.5,
        permission_timeout=0.1,
    ).execute(prompt="x", model="openai/gpt", session_id=None)

    assert result.stdout == ""
    assert client.replies == [("per_1", "reject", TIMEOUT_MESSAGE)]
    assert len(resolved) == 1
    assert resolved[0][0] == "per_1"
    assert resolved[0][1].timed_out is True
    assert resolved[0][1].cancelled is False


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Poll a condition without assuming a fixed number of loop iterations."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met before the timeout")
        await asyncio.sleep(0.005)


class _GateHandler:
    """Permission handler whose answers are released one at a time by the test."""

    def __init__(self) -> None:
        self.requests: list[PermissionRequest] = []
        self.gates: dict[str, asyncio.Event] = {}

    async def __call__(self, request: PermissionRequest) -> PermissionDecision:
        self.requests.append(request)
        gate = asyncio.Event()
        self.gates[request.id] = gate
        await gate.wait()
        return PermissionDecision(reply="once")

    def answered(self, request_id: str) -> bool:
        return request_id in self.gates


class _RecordingPresenter:
    """Presenter double that records every resolution."""

    def __init__(self) -> None:
        self.resolved: list[tuple[str, PermissionDecision]] = []

    async def show(self, request: PermissionRequest) -> None:
        return None

    async def resolve(self, request_id: str, decision: PermissionDecision) -> None:
        self.resolved.append((request_id, decision))


async def test_two_pending_permissions_survive_the_first_resolution() -> None:
    """CRITICAL regression: settling one dialog must not abort the turn.

    ``_wait_with_permission`` used to raise ``TimeoutError`` whenever a
    permission settled while another was still pending, so answering the first
    dialog cancelled the second unanswered and failed the turn with a bogus
    "did not finish in Ns" error. ``permission_timeout=0`` (D4) makes it worse:
    "wait forever" still aborted on the first settled permission.
    """
    handler = _GateHandler()
    client = FakeClient(streams=[[_permission("per_1"), _permission("per_2"), _Hang(), _idle()]])
    backend = _backend(client, permission_handler=handler, timeout=5, permission_timeout=0)

    task = asyncio.create_task(backend.execute(prompt="x", model="openai/gpt", session_id=None))
    try:
        await _wait_until(lambda: handler.answered("per_1") and handler.answered("per_2"))

        handler.gates["per_1"].set()
        await _wait_until(lambda: ("per_1", "once", None) in client.replies)
        await asyncio.sleep(0.05)
        assert not task.done(), "the turn must survive while per_2 is still pending"

        handler.gates["per_2"].set()
        await _wait_until(lambda: ("per_2", "once", None) in client.replies)
        client.release()
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            backend.cancel()
            with contextlib.suppress(BaseException):
                await task

    assert result.cancelled is False
    assert client.replies == [("per_1", "once", None), ("per_2", "once", None)]
    assert client.aborted == []


async def test_two_pending_permissions_survive_with_a_positive_timeout() -> None:
    """Same multi-permission contract with the default kind of budget."""
    handler = _GateHandler()
    client = FakeClient(streams=[[_permission("per_1"), _permission("per_2"), _Hang(), _idle()]])
    backend = _backend(client, permission_handler=handler, timeout=5, permission_timeout=5)

    task = asyncio.create_task(backend.execute(prompt="x", model="openai/gpt", session_id=None))
    try:
        await _wait_until(lambda: handler.answered("per_1") and handler.answered("per_2"))

        handler.gates["per_1"].set()
        await _wait_until(lambda: ("per_1", "once", None) in client.replies)
        await asyncio.sleep(0.05)
        assert not task.done(), "the turn must survive while per_2 is still pending"

        handler.gates["per_2"].set()
        await _wait_until(lambda: ("per_2", "once", None) in client.replies)
        client.release()
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            backend.cancel()
            with contextlib.suppress(BaseException):
                await task

    assert result.cancelled is False
    assert client.replies == [("per_1", "once", None), ("per_2", "once", None)]
    assert client.aborted == []


async def test_backend_cancel_notifies_the_router_presenters() -> None:
    """End-to-end: backend ``_finish_permissions(cancelled=True)`` → UI notice.

    The original bug was the backend cancelling the handler task and nobody
    telling the presenters. This drives the real chain (backend -> router ->
    ``_resolve_all``) with only a fake client, no live server.
    """
    presenter = _RecordingPresenter()
    router = PermissionRouter(presenters=[presenter], timeout=0)
    client = FakeClient(streams=[[_permission(), _Hang()]])
    backend = _backend(
        client,
        permission_handler=router.request_permission,
        timeout=5,
        permission_timeout=0,
    )

    task = asyncio.create_task(backend.execute(prompt="x", model="openai/gpt", session_id=None))
    await _wait_until(lambda: router.pending_count == 1)

    backend.cancel()
    result = await asyncio.wait_for(task, timeout=2)

    assert result.cancelled is True
    assert router.pending_count == 0
    assert len(presenter.resolved) == 1
    request_id, decision = presenter.resolved[0]
    assert request_id == "per_1"
    assert decision.cancelled is True
    assert decision.timed_out is False


async def test_backend_permission_budget_expiry_is_reported_as_expired() -> None:
    """Defense-in-depth expiry must read as "expired", not "turn interrupted"."""
    presenter = _RecordingPresenter()
    # The router waits forever: only the backend's own budget can fire.
    router = PermissionRouter(presenters=[presenter], timeout=0)
    client = FakeClient(streams=[[_permission(), _Hang()]])
    backend = _backend(
        client,
        permission_handler=router.request_permission,
        timeout=5,
        permission_timeout=0.1,
    )

    with pytest.raises(OpenCodeStreamError, match="permiso"):
        await backend.execute(prompt="x", model="openai/gpt", session_id=None)

    assert router.pending_count == 0
    assert len(presenter.resolved) == 1
    _, decision = presenter.resolved[0]
    assert decision.cancelled is True
    assert decision.timed_out is True


# ── Permission budget is absolute, not per-wait ────────────────────────────


def test_permission_budget_deadline_arms_once() -> None:
    """The budget must not restart every time a permission settles (#6)."""
    budget = _PermissionBudget(timeout=10.0)

    assert budget.remaining(100.0) == pytest.approx(11.0)  # 10 + grace
    assert budget.remaining(105.0) == pytest.approx(6.0)
    assert budget.remaining(105.5) == pytest.approx(5.5)  # NOT re-armed to 11


def test_permission_budget_is_rearmed_after_the_spell_ends() -> None:
    budget = _PermissionBudget(timeout=10.0)

    assert budget.remaining(100.0) == pytest.approx(11.0)
    budget.clear()
    assert budget.remaining(200.0) == pytest.approx(11.0)


def test_zero_permission_budget_is_infinite() -> None:
    budget = _PermissionBudget(timeout=0)

    assert budget.remaining(100.0) is None
    assert budget.remaining(1_000_000.0) is None


# ── Dedup + reconnect ─────────────────────────────────────────────────────


async def test_repeated_event_ids_are_applied_once() -> None:
    client = FakeClient(
        streams=[[_delta("X", event_id="evt_1"), _delta("X", event_id="evt_1"), _idle()]]
    )

    result = await _backend(client).execute(prompt="x", model="openai/gpt", session_id=None)

    assert result.stdout == "X"


async def test_reconnect_backoff_reconciles_pending_permissions() -> None:
    requests: list[PermissionRequest] = []

    async def handler(request: PermissionRequest) -> PermissionDecision:
        requests.append(request)
        return PermissionDecision(reply="once")

    pending = PermissionRequest(
        id="per_reconciled",
        session_id=SID,
        permission="bash",
        patterns=("git push *",),
        metadata={},
        always=(),
        message_id=None,
        call_id=None,
    )
    client = FakeClient(
        streams=[
            OpenCodeUnavailableError("stream dropped"),
            [_delta("recuperado"), _idle()],
        ],
        permissions=[pending],
    )

    result = await _backend(
        client,
        permission_handler=handler,
        reconnect_initial=0.01,
        reconnect_max=0.02,
    ).execute(prompt="x", model="openai/gpt", session_id=None)

    assert result.stdout == "recuperado"
    assert client.stream_count == 2
    assert client.permission_list_calls >= 1
    assert [request.id for request in requests] == ["per_reconciled"]
    assert client.replies == [("per_reconciled", "once", None)]


# -- Directory scoping (regression: silent hang on the wrong workdir) ------

WORKDIR = "C:/conv/project"


async def test_workdir_is_forwarded_to_session_stream_and_prompt() -> None:
    client = FakeClient(streams=[[_delta("ok"), _idle()]])

    result = await _backend(client).execute(
        prompt="x",
        model="openai/gpt",
        session_id=None,
        workdir=WORKDIR,
    )

    assert result.stdout == "ok"
    assert client.directories["create"] == [WORKDIR]
    assert client.directories["stream"] == [WORKDIR]
    assert client.directories["prompt"] == [WORKDIR]


async def test_fallback_and_permission_reply_use_the_same_workdir() -> None:
    async def handler(request: PermissionRequest) -> PermissionDecision:
        return PermissionDecision(reply="once")

    client = FakeClient(streams=[[_permission(), _idle()]], messages=[])

    result = await _backend(client, permission_handler=handler).execute(
        prompt="x",
        model="openai/gpt",
        session_id=None,
        workdir=WORKDIR,
    )

    assert result.stdout == ""
    assert client.directories["reply"] == [WORKDIR]
    assert client.directories["messages"] == [WORKDIR]


async def test_abort_uses_the_workdir() -> None:
    client = FakeClient(streams=[[_delta("a"), _Hang()]])

    with pytest.raises(OpenCodeStreamError):
        await _backend(client, first_event_timeout=5.0).execute(
            prompt="x",
            model="openai/gpt",
            session_id=None,
            workdir=WORKDIR,
            timeout=1,
        )

    assert client.directories["abort"] == [WORKDIR]


# -- Bounded setup: never hang silently -----------------------------------


class _NeverConnectClient(FakeClient):
    def stream_events(
        self,
        *,
        on_connect: Any = None,
        directory: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        self.directories["stream"].append(directory)
        return self._never(on_connect)

    async def _never(self, on_connect: Any) -> AsyncIterator[dict[str, Any]]:
        await asyncio.sleep(3600)
        yield {}  # pragma: no cover


async def test_sse_connect_timeout_raises_instead_of_hanging() -> None:
    client = _NeverConnectClient()
    backend = _backend(client, connect_timeout=0.05)

    started = asyncio.get_running_loop().time()
    with pytest.raises(OpenCodeStreamError):
        await backend.execute(prompt="x", model="openai/gpt", session_id=None, workdir=WORKDIR)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 1.0
    assert client.prompts == []


class _HangingCreateClient(FakeClient):
    async def create_session(self, title: str, *, directory: str | None = None) -> str:
        await asyncio.sleep(3600)
        return SID  # pragma: no cover


async def test_create_session_timeout_raises_typed_error() -> None:
    client = _HangingCreateClient()
    backend = _backend(client, request_timeout=0.05)

    with pytest.raises(OpenCodeUnavailableError) as exc_info:
        await backend.execute(prompt="x", model="openai/gpt", session_id=None)

    assert "0.05s" in str(exc_info.value)


class _HangingPromptClient(FakeClient):
    async def send_prompt(
        self,
        session_id: str,
        prompt: str,
        *,
        model: str,
        agent: str | None = None,
        directory: str | None = None,
    ) -> None:
        self.directories["prompt"].append(directory)
        await asyncio.sleep(3600)


async def test_send_prompt_timeout_raises_typed_error() -> None:
    client = _HangingPromptClient(streams=[[_Hang()]])
    backend = _backend(client, request_timeout=0.05)

    with pytest.raises(OpenCodeUnavailableError) as exc_info:
        await backend.execute(prompt="x", model="openai/gpt", session_id=None, workdir=WORKDIR)

    assert "0.05s" in str(exc_info.value)


async def test_no_event_after_prompt_raises_stream_error() -> None:
    """The old backend waited the full idle timeout (1200s) in silence."""
    client = FakeClient(streams=[[_Hang()]])
    backend = _backend(client, first_event_timeout=0.05)

    started = asyncio.get_running_loop().time()
    with pytest.raises(OpenCodeStreamError):
        await backend.execute(
            prompt="x",
            model="openai/gpt",
            session_id=None,
            workdir=WORKDIR,
            timeout=5,
        )
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 1.0
    assert client.directories["stream"] == [WORKDIR]
    assert client.directories["prompt"] == [WORKDIR]
