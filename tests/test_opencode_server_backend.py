"""Tests for ``OpenCodeServerBackend`` — no live server.

A fake client plays a scripted SSE sequence so every edge of the streaming
contract is deterministic: stream-before-prompt ordering, text accumulation,
progress throttling, idle termination, the ``get_messages`` fallback, the
idle-reset timeout, cancellation, the fail-closed permission stub, event-id
deduplication, and reconnection with permission reconciliation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from services.opencode_server.errors import OpenCodeUnavailableError
from services.opencode_server_backend import (
    PHASE3_REJECT_MESSAGE,
    OpenCodeServerBackend,
)
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
        self._release = asyncio.Event()

    def release(self) -> None:
        self._release.set()

    async def create_session(self, title: str) -> str:
        self.order.append("create")
        self.created_titles.append(title)
        return self.session_id

    def stream_events(self, *, on_connect: Any = None) -> AsyncIterator[dict[str, Any]]:
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
    ) -> None:
        self.order.append("prompt")
        self.prompts.append((session_id, prompt, agent))

    async def get_messages(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        self.message_requests.append(session_id)
        return self.messages

    async def list_permissions(self) -> list[PermissionRequest]:
        self.permission_list_calls += 1
        return self.permissions

    async def reply_permission(
        self,
        request_id: str,
        reply: str,
        message: str | None = None,
    ) -> bool:
        self.replies.append((request_id, reply, message))
        return True

    async def abort(self, session_id: str) -> bool:
        self.aborted.append(session_id)
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

        async def ensure_running(self) -> None:
            self.calls += 1

    manager = FakeManager()
    client = FakeClient(streams=[[_delta("a"), _idle()], [_delta("b"), _idle()]])
    backend = _backend(client, manager=manager)

    assert manager.calls == 0
    await backend.execute(prompt="a", model="openai/gpt", session_id=None)
    await backend.execute(prompt="b", model="openai/gpt", session_id=None)

    assert manager.calls == 1


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


async def test_idle_timeout_without_events_aborts() -> None:
    client = FakeClient(streams=[[_delta("a"), _Hang()]])

    result = await _backend(client).execute(
        prompt="x",
        model="openai/gpt",
        session_id=None,
        timeout=1,
    )

    assert result.timed_out is True
    assert result.returncode == -1
    assert result.stdout == "a"
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
