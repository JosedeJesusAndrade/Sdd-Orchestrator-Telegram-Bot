"""Tests for the Telegram permission callback handler.

The handler is intentionally thin: parse ``perm:<id>:<reply>``, delegate the
decision to the router, answer the toast, and NEVER edit the message (the
presenter's ``resolve`` owns that, and a second edit would race it).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from telegram import Update
from telegram.ext import ContextTypes

from config import ALLOWED_CHAT_IDS, CONTAINER_KEY
from handlers.permissions import (
    ALREADY_RESOLVED_TOAST,
    INVALID_CALLBACK_TOAST,
    permission_callback,
)
from services.permissions import PermissionDecision, Reply


class _FakeQuery:
    def __init__(self, data: str, *, answer_error: Exception | None = None) -> None:
        self.data = data
        self.answers: list[str | None] = []
        self.edits = 0
        self._answer_error = answer_error

    async def answer(self, text: str | None = None, **kwargs: Any) -> None:
        if self._answer_error is not None:
            raise self._answer_error
        self.answers.append(text)

    async def edit_message_text(self, *args: Any, **kwargs: Any) -> None:
        self.edits += 1


class _FakeRouter:
    def __init__(self, result: bool = True) -> None:
        self.calls: list[tuple[str, PermissionDecision]] = []
        self._result = result

    async def resolve_from_ui(self, request_id: str, decision: PermissionDecision) -> bool:
        self.calls.append((request_id, decision))
        return self._result


def _build(
    data: str,
    *,
    router: _FakeRouter | None = None,
    chat_id: int | None = None,
    answer_error: Exception | None = None,
) -> tuple[Any, _FakeQuery, Any]:
    query = _FakeQuery(data, answer_error=answer_error)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id if chat_id is not None else ALLOWED_CHAT_IDS[0]),
        callback_query=query,
    )
    container = SimpleNamespace(permission_router=router)
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={CONTAINER_KEY: container}),
    )
    return update, query, context


async def _run(update: Any, context: Any) -> None:
    await permission_callback(
        cast(Update, update),
        cast(ContextTypes.DEFAULT_TYPE, context),
    )


# ── Happy path ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("reply", "toast"),
    [
        ("once", "✅ Permitido"),
        ("always", "✅ Permitido (siempre)"),
        ("reject", "❌ Rechazado"),
    ],
)
async def test_callback_parses_and_resolves(reply: str, toast: str) -> None:
    router = _FakeRouter()
    update, query, context = _build(f"perm:per_1:{reply}", router=router)

    await _run(update, context)

    assert router.calls == [("per_1", PermissionDecision(reply=cast(Reply, reply)))]
    assert query.answers == [toast]
    # The handler must NOT edit the message: the presenter owns the edit.
    assert query.edits == 0
    assert len(router.calls) == 1, "the router must be resolved exactly once"


# ── Guards ────────────────────────────────────────────────────────────────


async def test_already_resolved_answers_ya_fue_resuelto() -> None:
    router = _FakeRouter(result=False)
    update, query, context = _build("perm:per_1:once", router=router)

    await _run(update, context)

    assert router.calls == [("per_1", PermissionDecision(reply="once"))]
    assert query.answers == [ALREADY_RESOLVED_TOAST]
    assert query.edits == 0


async def test_missing_router_answers_ya_fue_resuelto() -> None:
    update, query, context = _build("perm:per_1:once", router=None)

    await _run(update, context)

    assert query.answers == [ALREADY_RESOLVED_TOAST]
    assert query.edits == 0


@pytest.mark.parametrize(
    "data",
    [
        "perm:per_1",  # missing reply
        "perm::once",  # empty request id
        "perm:per_1:maybe",  # invalid reply
        "other:per_1:once",  # wrong prefix
        "",  # empty
    ],
)
async def test_malformed_callback_is_answered_not_crashed(data: str) -> None:
    router = _FakeRouter()
    update, query, context = _build(data, router=router)

    await _run(update, context)

    assert query.answers == [INVALID_CALLBACK_TOAST]
    assert router.calls == []


async def test_unauthorized_chat_is_ignored() -> None:
    router = _FakeRouter()
    update, query, context = _build(
        "perm:per_1:once",
        router=router,
        chat_id=-999_999_999,
    )

    await _run(update, context)

    assert query.answers == []
    assert router.calls == []


async def test_answer_failure_does_not_crash() -> None:
    router = _FakeRouter()
    update, query, context = _build(
        "perm:per_1:once",
        router=router,
        answer_error=RuntimeError("query is too old"),
    )

    await _run(update, context)

    assert router.calls == [("per_1", PermissionDecision(reply="once"))]
