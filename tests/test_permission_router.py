"""Tests for ``PermissionRouter`` — no live server, no real UI.

Covers the rendezvous contract: broadcast to every presenter, first-wins,
timeout -> auto-reject + notice, fail-closed without presenters, presenter
isolation, concurrent independent requests, and future cleanup.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from services.permission_router import (
    CANCELLED_MESSAGE,
    NO_PRESENTER_MESSAGE,
    TIMEOUT_MESSAGE,
    PermissionRouter,
)
from services.permissions import PermissionDecision, PermissionRequest

SID = "ses_router_test"


def _request(request_id: str = "per_1", *, session_id: str = SID) -> PermissionRequest:
    return PermissionRequest(
        id=request_id,
        session_id=session_id,
        permission="bash",
        patterns=("git commit *",),
        metadata={"command": "git commit -m x"},
        always=("git commit *",),
        message_id="msg_1",
        call_id="call_1",
    )


class RecordingPresenter:
    """Minimal presenter double; the optional queue makes concurrency deterministic."""

    def __init__(self, queue: asyncio.Queue[PermissionRequest] | None = None) -> None:
        self.shown: list[PermissionRequest] = []
        self.resolved: list[tuple[str, PermissionDecision]] = []
        self.shown_event = asyncio.Event()
        self._queue = queue

    async def show(self, request: PermissionRequest) -> None:
        self.shown.append(request)
        self.shown_event.set()
        if self._queue is not None:
            self._queue.put_nowait(request)

    async def resolve(self, request_id: str, decision: PermissionDecision) -> None:
        self.resolved.append((request_id, decision))


class RaisingPresenter:
    async def show(self, request: PermissionRequest) -> None:
        raise RuntimeError("presenter roto")

    async def resolve(self, request_id: str, decision: PermissionDecision) -> None:
        raise RuntimeError("presenter roto")


# ── Happy path ────────────────────────────────────────────────────────────


async def test_presenter_receives_the_request() -> None:
    request = _request()
    presenter = RecordingPresenter()
    router = PermissionRouter(presenters=[presenter], timeout=1.0)

    task = asyncio.create_task(router.request_permission(request))
    await asyncio.wait_for(presenter.shown_event.wait(), timeout=1.0)

    assert presenter.shown == [request]
    assert router.pending_count == 1

    assert await router.resolve_from_ui(request.id, PermissionDecision(reply="once")) is True

    assert await task == PermissionDecision(reply="once")
    assert router.pending_count == 0


async def test_resolve_from_ui_returns_the_decision() -> None:
    request = _request()
    presenter = RecordingPresenter()
    router = PermissionRouter(presenters=[presenter], timeout=1.0)
    expected = PermissionDecision(reply="always", message="ok")

    task = asyncio.create_task(router.request_permission(request))
    await asyncio.wait_for(presenter.shown_event.wait(), timeout=1.0)

    assert await router.resolve_from_ui(request.id, expected) is True
    assert await task == expected


async def test_first_resolve_wins_and_duplicates_are_ignored() -> None:
    request = _request()
    presenter = RecordingPresenter()
    router = PermissionRouter(presenters=[presenter], timeout=1.0)

    task = asyncio.create_task(router.request_permission(request))
    await asyncio.wait_for(presenter.shown_event.wait(), timeout=1.0)

    first = PermissionDecision(reply="once")
    assert await router.resolve_from_ui(request.id, first) is True
    assert await router.resolve_from_ui(request.id, PermissionDecision(reply="reject")) is False

    assert await task == first
    assert router.pending_count == 0


async def test_unknown_request_id_is_rejected_by_resolve() -> None:
    router = PermissionRouter(presenters=[RecordingPresenter()], timeout=1.0)

    assert await router.resolve_from_ui("per_unknown", PermissionDecision(reply="once")) is False


async def test_ui_resolution_notifies_presenters() -> None:
    """The router (not the callback handler) tells UIs how the request ended."""
    request = _request()
    presenter = RecordingPresenter()
    router = PermissionRouter(presenters=[presenter], timeout=1.0)

    task = asyncio.create_task(router.request_permission(request))
    await asyncio.wait_for(presenter.shown_event.wait(), timeout=1.0)

    decision = PermissionDecision(reply="always")
    assert await router.resolve_from_ui(request.id, decision) is True

    assert presenter.resolved == [(request.id, decision)]
    assert await task == decision


# ── Observability ─────────────────────────────────────────────────────────


async def test_request_permission_logs_the_asked_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = _request()
    presenter = RecordingPresenter()
    router = PermissionRouter(presenters=[presenter], timeout=1.0)

    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(router.request_permission(request))
        await asyncio.wait_for(presenter.shown_event.wait(), timeout=1.0)
        assert await router.resolve_from_ui(request.id, PermissionDecision(reply="once")) is True
        await task

    asked = [
        record for record in caplog.records if getattr(record, "event", "") == "permission_asked"
    ]
    assert len(asked) == 1
    record = asked[0]
    assert getattr(record, "request_id", None) == request.id
    assert getattr(record, "session_id", None) == request.session_id
    assert getattr(record, "action", None) == "bash"
    assert getattr(record, "patterns", None) == "git commit *"
    assert getattr(record, "command", None) == "git commit -m x"
    assert getattr(record, "presenters", None) == 1


async def test_permission_asked_redacts_obvious_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = PermissionRequest(
        id="per_secret",
        session_id=SID,
        permission="bash",
        patterns=("mysql *",),
        metadata={"command": "mysql --password=supersecret -u root", "agent": "build"},
        always=(),
        message_id=None,
        call_id=None,
    )
    presenter = RecordingPresenter()
    router = PermissionRouter(presenters=[presenter], timeout=1.0)

    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(router.request_permission(request))
        await asyncio.wait_for(presenter.shown_event.wait(), timeout=1.0)
        await router.resolve_from_ui(request.id, PermissionDecision(reply="reject"))
        await task

    record = next(
        item for item in caplog.records if getattr(item, "event", "") == "permission_asked"
    )
    assert getattr(record, "command", None) == "mysql --password=*** -u root"
    assert getattr(record, "agent", None) == "build"
    assert "supersecret" not in str(getattr(record, "command", ""))


async def test_resolve_from_ui_logs_the_resolved_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = _request()
    presenter = RecordingPresenter()
    router = PermissionRouter(presenters=[presenter], timeout=1.0)

    task = asyncio.create_task(router.request_permission(request))
    await asyncio.wait_for(presenter.shown_event.wait(), timeout=1.0)

    with caplog.at_level(logging.INFO):
        assert await router.resolve_from_ui(request.id, PermissionDecision(reply="always")) is True
        await task

    resolved = [
        record for record in caplog.records if getattr(record, "event", "") == "permission_resolved"
    ]
    assert len(resolved) == 1
    assert getattr(resolved[0], "request_id", None) == request.id
    assert getattr(resolved[0], "reply", None) == "always"
    assert getattr(resolved[0], "timed_out", None) is False
    assert getattr(resolved[0], "via", None) == "ui"


# ── Timeout ───────────────────────────────────────────────────────────────


async def test_timeout_rejects_and_notifies_presenters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = _request()
    presenter = RecordingPresenter()
    router = PermissionRouter(presenters=[presenter], timeout=0.01)

    with caplog.at_level(logging.INFO):
        decision = await asyncio.wait_for(router.request_permission(request), timeout=1.0)

    assert decision == PermissionDecision(
        reply="reject",
        message=TIMEOUT_MESSAGE,
        timed_out=True,
    )
    assert presenter.resolved == [(request.id, decision)]
    assert router.pending_count == 0
    assert any(getattr(record, "event", "") == "permission_timeout" for record in caplog.records)

    timeout_records = [
        record for record in caplog.records if getattr(record, "event", "") == "permission_timeout"
    ]
    assert len(timeout_records) == 1
    assert getattr(timeout_records[0], "request_id", None) == request.id

    resolved = [
        record for record in caplog.records if getattr(record, "event", "") == "permission_resolved"
    ]
    assert len(resolved) == 1
    assert getattr(resolved[0], "request_id", None) == request.id
    assert getattr(resolved[0], "reply", None) == "reject"
    assert getattr(resolved[0], "timed_out", None) is True
    assert getattr(resolved[0], "via", None) == "timeout"


# ── Fail closed / isolation ───────────────────────────────────────────────


async def test_no_presenters_fails_closed_immediately(
    caplog: pytest.LogCaptureFixture,
) -> None:
    router = PermissionRouter(presenters=[], timeout=10.0)

    with caplog.at_level(logging.INFO):
        decision = await asyncio.wait_for(router.request_permission(_request()), timeout=1.0)

    assert decision.reply == "reject"
    assert decision.message == NO_PRESENTER_MESSAGE
    assert decision.timed_out is False
    assert router.pending_count == 0
    assert any(
        getattr(record, "event", "") == "permission_no_presenter" for record in caplog.records
    )

    resolved = [
        record for record in caplog.records if getattr(record, "event", "") == "permission_resolved"
    ]
    assert len(resolved) == 1
    assert getattr(resolved[0], "reply", None) == "reject"
    assert getattr(resolved[0], "timed_out", None) is False
    assert getattr(resolved[0], "via", None) == "no_presenter"


async def test_a_broken_presenter_does_not_block_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = _request()
    healthy = RecordingPresenter()
    router = PermissionRouter(presenters=[RaisingPresenter(), healthy], timeout=1.0)

    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(router.request_permission(request))
        await asyncio.wait_for(healthy.shown_event.wait(), timeout=1.0)

        assert await router.resolve_from_ui(request.id, PermissionDecision(reply="once")) is True
        assert await task == PermissionDecision(reply="once")

    assert any(
        getattr(record, "event", "") == "permission_presenter_show_failed"
        for record in caplog.records
    )


# ── Concurrency ───────────────────────────────────────────────────────────


# ── Zero timeout = wait for the human indefinitely ───────────────────────


async def test_zero_timeout_waits_indefinitely_for_the_human() -> None:
    """``OPENCODE_PERMISSION_TIMEOUT=0`` disables the auto-reject safety net."""
    request = _request()
    presenter = RecordingPresenter()
    router = PermissionRouter(presenters=[presenter], timeout=0)

    task = asyncio.create_task(router.request_permission(request))
    await asyncio.wait_for(presenter.shown_event.wait(), timeout=1.0)
    await asyncio.sleep(0.05)

    assert not task.done(), "a zero timeout must never auto-reject"

    assert await router.resolve_from_ui(request.id, PermissionDecision(reply="once")) is True
    assert await task == PermissionDecision(reply="once")
    assert router.pending_count == 0


# ── Cancellation = notify every presenter ────────────────────────────────


async def test_cancellation_notifies_presenters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The backend can cancel the task; the UIs must still close their dialogs."""
    request = _request()
    presenter = RecordingPresenter()
    router = PermissionRouter(presenters=[presenter], timeout=10.0)

    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(router.request_permission(request))
        await asyncio.wait_for(presenter.shown_event.wait(), timeout=1.0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    expected = PermissionDecision(
        reply="reject",
        message=CANCELLED_MESSAGE,
        cancelled=True,
    )
    assert presenter.resolved == [(request.id, expected)]
    assert router.pending_count == 0
    assert any(getattr(record, "event", "") == "permission_cancelled" for record in caplog.records)


async def test_concurrent_requests_are_independent() -> None:
    first = _request("per_1")
    second = _request("per_2")
    shown: asyncio.Queue[PermissionRequest] = asyncio.Queue()
    presenter = RecordingPresenter(queue=shown)
    router = PermissionRouter(presenters=[presenter], timeout=1.0)

    task_one = asyncio.create_task(router.request_permission(first))
    task_two = asyncio.create_task(router.request_permission(second))
    await asyncio.wait_for(shown.get(), timeout=1.0)
    await asyncio.wait_for(shown.get(), timeout=1.0)

    assert router.pending_count == 2

    assert await router.resolve_from_ui(first.id, PermissionDecision(reply="once")) is True
    assert await task_one == PermissionDecision(reply="once")
    assert router.pending_count == 1
    assert not task_two.done()

    assert await router.resolve_from_ui(second.id, PermissionDecision(reply="always")) is True
    assert await task_two == PermissionDecision(reply="always")
    assert router.pending_count == 0
