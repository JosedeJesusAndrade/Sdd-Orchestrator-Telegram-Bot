"""Tests for the Flet permission presenter (no real Flet client).

The presenter is a pure control builder plus a small queue; these tests use a
``FakePage`` that records ``show_dialog`` / ``pop_dialog`` / ``update`` calls,
following the harness style of ``tests/test_flet_render.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

import flet as ft
import pytest

from flet_app.flet_permission_presenter import (
    ALWAYS_LABEL,
    PERMIT_LABEL,
    REJECT_LABEL,
    RESULT_CANCELLED,
    RESULT_PERMITTED,
    RESULT_PERMITTED_ALWAYS,
    RESULT_REJECTED,
    RESULT_TIMED_OUT,
    FletPermissionPresenter,
    build_permission_dialog,
    result_text,
)
from services.permissions import PermissionDecision, PermissionRequest


class FakePage:
    """Minimal ``ft.Page`` stand-in recording dialog calls."""

    def __init__(self) -> None:
        self.dialogs: list[ft.AlertDialog] = []
        self.popped = 0
        self.updates = 0
        self.show_error: Exception | None = None

    def show_dialog(self, dialog: ft.AlertDialog) -> None:
        if self.show_error is not None:
            raise self.show_error
        self.dialogs.append(dialog)

    def pop_dialog(self) -> ft.AlertDialog | None:
        self.popped += 1
        return None

    def update(self, *controls: ft.Control) -> None:
        self.updates += 1


class BrokenPopPage(FakePage):
    """Page whose dialog was already removed by the client."""

    def pop_dialog(self) -> ft.AlertDialog | None:
        raise RuntimeError("dialog is gone")


async def _resolve_true(request_id: str, decision: PermissionDecision) -> bool:
    return True


async def _noop_decide(request_id: str, decision: PermissionDecision) -> None:
    pass


def _request(
    request_id: str = "per_1",
    *,
    permission: str = "bash",
    patterns: tuple[str, ...] = ("git commit *",),
    command: str | None = 'git commit -m "hola"',
    always: tuple[str, ...] = ("git commit *",),
) -> PermissionRequest:
    metadata: dict[str, Any] = {} if command is None else {"command": command}
    return PermissionRequest(
        id=request_id,
        session_id="ses_1",
        permission=permission,
        patterns=patterns,
        metadata=metadata,
        always=always,
        message_id=None,
        call_id=None,
    )


def _presenter(
    page: FakePage | None = None,
    column: ft.Column | None = None,
) -> tuple[
    FletPermissionPresenter,
    FakePage,
    ft.Column,
    list[tuple[str, PermissionDecision]],
]:
    fake_page = page or FakePage()
    conversation = column or ft.Column()
    decisions: list[tuple[str, PermissionDecision]] = []

    async def resolve(request_id: str, decision: PermissionDecision) -> bool:
        decisions.append((request_id, decision))
        return True

    presenter = FletPermissionPresenter(
        cast(ft.Page, fake_page),
        conversation,
        resolve=resolve,
    )
    return presenter, fake_page, conversation, decisions


def _texts(control: ft.Control) -> list[str]:
    """Collect every ``ft.Text`` value in a control subtree."""
    values: list[str] = []
    title = getattr(control, "title", None)
    if isinstance(title, ft.Text):
        values.append(title.value or "")
    if isinstance(control, ft.Text):
        values.append(control.value or "")
    content = getattr(control, "content", None)
    if isinstance(content, ft.Control):
        values.extend(_texts(content))
    children = getattr(control, "controls", None)
    if children:
        for child in children:
            values.extend(_texts(child))
    return values


def _labels(dialog: ft.AlertDialog) -> list[str]:
    labels: list[str] = []
    for action in dialog.actions:
        content = getattr(action, "content", None)
        if isinstance(content, ft.Text):
            labels.append(content.value or "")
    return labels


async def _click(button: ft.TextButton) -> None:
    handler = cast(Callable[[Any], Awaitable[None]], button.on_click)
    await handler(None)


def _notice_value(column: ft.Column) -> str:
    notice = cast(ft.Container, column.controls[0])
    text = cast(ft.Text, notice.content)
    return text.value or ""


def test_dialog_has_details_and_three_actions() -> None:
    dialog = build_permission_dialog(_request(), _noop_decide)

    texts = _texts(dialog)
    assert "🔐 Permiso requerido" in texts
    assert "bash" in texts
    assert "git commit *" in texts
    assert 'git commit -m "hola"' in texts
    assert dialog.modal is True
    assert _labels(dialog) == [PERMIT_LABEL, ALWAYS_LABEL, REJECT_LABEL]


def test_multiple_patterns_are_rendered() -> None:
    dialog = build_permission_dialog(
        _request(patterns=("git commit *", "git push *"), always=()),
        _noop_decide,
    )

    assert "git commit *\ngit push *" in _texts(dialog)


async def test_show_calls_page_show_dialog() -> None:
    presenter, page, _, _ = _presenter()

    await presenter.show(_request())

    assert len(page.dialogs) == 1
    assert "git commit *" in _texts(page.dialogs[0])
    assert presenter.pending_count == 1


async def test_show_and_resolve_log_presenter_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    presenter, _, _, _ = _presenter()
    request = _request()

    with caplog.at_level(logging.INFO, logger="opencode_bot.flet_app.flet_permission_presenter"):
        await presenter.show(request)
        await presenter.resolve(request.id, PermissionDecision(reply="once"))

    shown = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "permission_presenter_show"
    ]
    resolved = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "permission_presenter_resolve"
    ]
    assert len(shown) == 1
    assert getattr(shown[0], "presenter", None) == "flet"
    assert getattr(shown[0], "request_id", None) == request.id
    assert getattr(shown[0], "queued", None) is False
    assert getattr(shown[0], "pending", None) == 1
    assert len(resolved) == 1
    assert getattr(resolved[0], "presenter", None) == "flet"
    assert getattr(resolved[0], "request_id", None) == request.id
    assert getattr(resolved[0], "reply", None) == "once"
    assert getattr(resolved[0], "was_queued", None) is False


async def test_queued_request_logs_that_it_was_queued(
    caplog: pytest.LogCaptureFixture,
) -> None:
    presenter, _, _, _ = _presenter()

    with caplog.at_level(logging.INFO, logger="opencode_bot.flet_app.flet_permission_presenter"):
        await presenter.show(_request("per_1"))
        await presenter.show(_request("per_2"))
        await presenter.resolve("per_2", PermissionDecision(reply="reject"))

    shown = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "permission_presenter_show"
    ]
    resolved = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "permission_presenter_resolve"
    ]
    assert [(item.__dict__["request_id"], item.__dict__["queued"]) for item in shown] == [
        ("per_1", False),
        ("per_2", True),
    ]
    assert len(resolved) == 1
    assert getattr(resolved[0], "request_id", None) == "per_2"
    assert getattr(resolved[0], "was_queued", None) is True


async def test_queue_shows_one_dialog_at_a_time() -> None:
    presenter, page, _, _ = _presenter()

    await presenter.show(_request("per_1"))
    await presenter.show(_request("per_2", command="rm -rf /tmp/x"))

    assert len(page.dialogs) == 1, "only one modal may be visible"
    assert presenter.pending_count == 2

    await presenter.resolve("per_1", PermissionDecision(reply="once"))

    assert len(page.dialogs) == 2
    assert "rm -rf /tmp/x" in _texts(page.dialogs[1])
    assert presenter.pending_count == 1


async def test_buttons_deliver_the_right_decisions() -> None:
    presenter, page, _, decisions = _presenter()
    request = _request()

    await presenter.show(request)
    for button, _expected in zip(
        page.dialogs[0].actions,
        ("once", "always", "reject"),
        strict=True,
    ):
        await _click(cast(ft.TextButton, button))

    assert [(request_id, decision.reply) for request_id, decision in decisions] == [
        (request.id, "once"),
        (request.id, "always"),
        (request.id, "reject"),
    ]


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (PermissionDecision(reply="once"), RESULT_PERMITTED),
        (PermissionDecision(reply="always"), RESULT_PERMITTED_ALWAYS),
        (PermissionDecision(reply="reject"), RESULT_REJECTED),
        (
            PermissionDecision(reply="reject", message="timeout", timed_out=True),
            RESULT_TIMED_OUT,
        ),
        (
            PermissionDecision(reply="reject", message="cancelled", cancelled=True),
            RESULT_CANCELLED,
        ),
        (
            PermissionDecision(reply="reject", message="expired", timed_out=True, cancelled=True),
            RESULT_TIMED_OUT,
        ),
    ],
)
async def test_resolve_pops_and_appends_the_notice(
    decision: PermissionDecision,
    expected: str,
) -> None:
    presenter, page, column, _ = _presenter()
    request = _request()

    await presenter.show(request)
    await presenter.resolve(request.id, decision)

    assert page.popped == 1
    assert len(column.controls) == 1
    assert _notice_value(column) == expected
    assert page.updates == 1
    assert presenter.pending_count == 0


async def test_result_text_maps_timeout_to_the_automatic_rejection() -> None:
    assert result_text(PermissionDecision(reply="reject", timed_out=True)) == RESULT_TIMED_OUT
    assert result_text(PermissionDecision(reply="reject")) == RESULT_REJECTED
    # Backend permission-budget expiry: cancelled by the backend, but the
    # correct message is "expired" (timed out waiting for the human).
    assert (
        result_text(PermissionDecision(reply="reject", timed_out=True, cancelled=True))
        == RESULT_TIMED_OUT
    )


def test_result_text_maps_cancellation_to_the_interrupted_turn() -> None:
    assert result_text(PermissionDecision(reply="reject", cancelled=True)) == RESULT_CANCELLED


async def test_duplicate_resolve_is_a_noop() -> None:
    presenter, page, column, _ = _presenter()
    request = _request()

    await presenter.show(request)
    await presenter.resolve(request.id, PermissionDecision(reply="once"))
    await presenter.resolve(request.id, PermissionDecision(reply="reject"))

    assert page.popped == 1, "the dialog must be popped exactly once"
    assert len(column.controls) == 1
    assert _notice_value(column) == RESULT_PERMITTED


async def test_queued_request_resolved_before_shown_is_never_displayed() -> None:
    presenter, page, column, _ = _presenter()

    await presenter.show(_request("per_1"))
    await presenter.show(_request("per_2"))
    await presenter.resolve("per_2", PermissionDecision(reply="reject", timed_out=True))

    assert page.popped == 0, "the visible dialog belongs to per_1"
    assert len(page.dialogs) == 1
    assert _notice_value(column) == RESULT_TIMED_OUT

    await presenter.resolve("per_1", PermissionDecision(reply="once"))
    assert len(page.dialogs) == 1, "the timed-out request was removed from the queue"


async def test_missing_page_does_not_crash() -> None:
    column = ft.Column()
    presenter = FletPermissionPresenter(
        cast(ft.Page, None),
        column,
        resolve=_resolve_true,
    )

    await presenter.show(_request())
    await presenter.resolve("per_1", PermissionDecision(reply="once"))

    assert column.controls == []
    assert presenter.pending_count == 0


async def test_pop_failure_is_swallowed_but_the_notice_is_appended() -> None:
    page = BrokenPopPage()
    presenter, _, column, _ = _presenter(page=page)
    request = _request()

    await presenter.show(request)
    await presenter.resolve(request.id, PermissionDecision(reply="reject"))

    assert len(column.controls) == 1
    assert _notice_value(column) == RESULT_REJECTED
