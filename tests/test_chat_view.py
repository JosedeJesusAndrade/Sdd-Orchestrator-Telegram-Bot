"""Tests for the ChatView seam.

A pure in-memory ChatView drives PromptService end-to-end with NO PTB
object anywhere on the core path. TelegramChatView is tested separately
against a fake MessageSender to prove it delegates (no re-implemented
formatting/fallback logic).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from services.ai_backend import AIBackendResult
from services.bot_port import MessageInfo
from services.chat_view import ChatView, ConversationId, ProgressHandle
from services.prompt_service import PromptService
from services.session_store import SessionInfo, SessionStore
from services.telegram_chat_view import TelegramChatView


# ── Fakes (core, frontend-agnostic) ────────────────────────────────────

class FakeStore:
    def __init__(self, settings: dict | None = None) -> None:
        self.increments = 0
        self._settings = settings or {}

    async def get_model(self, conversation_id: ConversationId) -> str:
        return "test/model"

    async def get_active_session(
        self, conversation_id: ConversationId
    ) -> SessionInfo | None:
        return SessionInfo(
            name="default", real_id=None, title="default",
            created="", last_used=None, prompt_count=0,
        )

    async def get_chat_setting(
        self, conversation_id: ConversationId, key: str, default: Any = None
    ) -> Any:
        return self._settings.get(key, default)

    async def increment_prompt_count(self, conversation_id: ConversationId) -> int:
        self.increments += 1
        return self.increments

    async def update_session_id(
        self, conversation_id: ConversationId, real_id: str
    ) -> None:
        return None


class FakeBackend:
    def __init__(self, result: AIBackendResult, gate: asyncio.Event | None = None) -> None:
        self._result = result
        self._gate = gate
        self.source_label: str | None = None
        self.timeout: int | None = None

    async def execute(
        self, prompt: str, model: str, session_id: str | None,
        agent: str | None = None, workdir: str | None = None,
        source_label: str = "",
        timeout: int | None = None,
    ) -> AIBackendResult:
        self.source_label = source_label
        self.timeout = timeout
        if self._gate is not None:
            await self._gate.wait()
        return self._result

    def cancel(self) -> None:
        return None


class FakeFactory:
    def __init__(self, backend: FakeBackend) -> None:
        self._backend = backend

    def create(self, provider: str | None = None, **kwargs):
        return self._backend


class FakeProgress:
    def __init__(self, view: "FakeChatView", text: str) -> None:
        self._view = view
        self.opening_text = text
        self.updates: list[str] = []
        self.finished: list[str] = []
        self.errors: list[str] = []

    async def update(self, text: str) -> None:
        self.updates.append(text)
        self._view.first_update.set()

    async def finish(self, text: str) -> None:
        self.finished.append(text)

    async def error(self, text: str) -> None:
        self.errors.append(text)


class FakeChatView:
    source_label = "\U0001f9ea fake"  # "🧪 fake"

    def __init__(self, deliver: bool = True) -> None:
        self.deliver = deliver
        self.started: list[str] = []
        self.sent: list[str] = []
        self.handles: list[FakeProgress] = []
        self.first_update = asyncio.Event()

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return self.deliver

    async def start_progress(self, text: str) -> ProgressHandle:
        self.started.append(text)
        handle = FakeProgress(self, text)
        self.handles.append(handle)
        return handle


# ── PromptService lifecycle (no PTB anywhere) ─────────────────────────

async def test_success_lifecycle_start_update_send_finish(monkeypatch) -> None:
    monkeypatch.setattr("services.prompt_service.PROGRESS_UPDATE_INTERVAL", 0.01)

    view = FakeChatView()
    backend = FakeBackend(
        AIBackendResult(stdout="Hello from the engine", returncode=0),
        gate=view.first_update,
    )
    service = PromptService(session_store=FakeStore(), provider_factory=FakeFactory(backend))

    result = await service.execute(conversation_id=123, prompt_text="hi", view=view)

    assert result == "Hello from the engine"
    assert view.started == ["\u23f3 OpenCode procesando..."]

    handle = view.handles[0]
    assert len(handle.updates) >= 1
    assert handle.updates[0] == "\u23f3 OpenCode procesando... (0s)"
    assert handle.finished == ["\u2705 Completado."]
    assert handle.errors == []

    assert view.sent, "final response must be delivered via the view"
    assert backend.source_label == view.source_label


async def test_failure_lifecycle_errors_without_send_or_finish() -> None:
    view = FakeChatView()
    backend = FakeBackend(
        AIBackendResult(stdout="", stderr="boom", returncode=1),
    )
    service = PromptService(session_store=FakeStore(), provider_factory=FakeFactory(backend))

    result = await service.execute(conversation_id=1, prompt_text="x", view=view)

    assert result == ""
    handle = view.handles[0]
    assert handle.errors and handle.errors[0].startswith("\u274c Error:")
    assert handle.finished == []
    assert view.sent == []


async def test_cancel_before_execution_calls_error_cancelado() -> None:
    view = FakeChatView()
    backend = FakeBackend(AIBackendResult(stdout="should not run", returncode=0))
    service = PromptService(session_store=FakeStore(), provider_factory=FakeFactory(backend))
    service._cancel.add(7)

    result = await service.execute(conversation_id=7, prompt_text="x", view=view)

    assert result == ""
    handle = view.handles[0]
    assert handle.errors == ["\u23f9\ufe0f Cancelado."]
    assert view.sent == []


async def test_cancel_before_execution_stops_progress_task(monkeypatch) -> None:
    """F2 regression: a pre-execution cancel must NOT leak the periodic updater.

    Before the fix, the early return skipped ``progress_task.cancel()``, so
    the leaked task kept calling ``update()`` and overwrote the terminal
    "⏹️ Cancelado." message with "⏳ OpenCode procesando... (Ns)".
    """
    monkeypatch.setattr("services.prompt_service.PROGRESS_UPDATE_INTERVAL", 0.01)

    view = FakeChatView()
    backend = FakeBackend(AIBackendResult(stdout="must not run", returncode=0))
    service = PromptService(session_store=FakeStore(), provider_factory=FakeFactory(backend))
    service._cancel.add(7)

    result = await service.execute(conversation_id=7, prompt_text="x", view=view)

    assert result == ""
    handle = view.handles[0]
    assert handle.errors == ["\u23f9\ufe0f Cancelado."]

    # If the updater had leaked, it would tick during this window.
    await asyncio.sleep(0.05)
    assert handle.updates == [], "no phantom progress ticks after cancel"


async def test_no_finish_when_delivery_fails() -> None:
    view = FakeChatView(deliver=False)
    backend = FakeBackend(AIBackendResult(stdout="content", returncode=0))
    service = PromptService(session_store=FakeStore(), provider_factory=FakeFactory(backend))

    await service.execute(conversation_id=1, prompt_text="x", view=view)

    handle = view.handles[0]
    assert view.sent, "send must still be attempted"
    assert handle.finished == [], "completion marker only after a delivered success"


def test_fakes_conform_to_protocols() -> None:
    view = FakeChatView()
    handle = FakeProgress(view, "x")
    assert isinstance(view, ChatView)
    assert isinstance(handle, ProgressHandle)


async def test_string_conversation_id_end_to_end(tmp_path) -> None:
    """F6: the core must not assume ``int`` conversation ids.

    A Flet-shaped synthetic string id drives PromptService through the REAL
    SessionStore end to end, with no int coercion anywhere on the core path.
    """
    store = SessionStore(tmp_path / "sessions.json")
    view = FakeChatView()
    backend = FakeBackend(AIBackendResult(stdout="ok", returncode=0))
    service = PromptService(session_store=store, provider_factory=FakeFactory(backend))

    result = await service.execute(
        conversation_id="flet-user-42", prompt_text="hi", view=view,
    )

    assert result == "ok"
    assert view.sent, "response delivered via the view"

    # The string id round-tripped through SessionStore with no int coercion
    # and was persisted under its own key.
    persisted = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
    assert "flet-user-42" in persisted
    assert await store.get_model("flet-user-42")  # default model read back by string id
    assert persisted["flet-user-42"]["sessions"]["default"]["prompt_count"] == 1


# ── F10: per-chat timeout is passed through to the backend ────────────

async def test_per_chat_timeout_is_passed_to_backend() -> None:
    """F10: the per-chat ``/config timeout`` setting must reach the backend.

    Before the fix, ``_execute_prompt`` computed ``timeout_val`` and dropped
    it on the floor, so the setting was advertised by ``/config`` but had
    zero effect (the backend always used its constructor timeout).
    """
    view = FakeChatView()
    backend = FakeBackend(AIBackendResult(stdout="ok", returncode=0))
    service = PromptService(
        session_store=FakeStore(settings={"timeout": 42}),
        provider_factory=FakeFactory(backend),
    )

    await service.execute(conversation_id=1, prompt_text="x", view=view)

    assert backend.timeout == 42


async def test_timeout_falls_back_to_global_when_unset() -> None:
    """With no per-chat setting, the global OPENCODE_TIMEOUT must be used."""
    from config import OPENCODE_TIMEOUT

    view = FakeChatView()
    backend = FakeBackend(AIBackendResult(stdout="ok", returncode=0))
    service = PromptService(
        session_store=FakeStore(),
        provider_factory=FakeFactory(backend),
    )

    await service.execute(conversation_id=1, prompt_text="x", view=view)

    assert backend.timeout == OPENCODE_TIMEOUT


# ── TelegramChatView adapter (delegation only) ────────────────────────

class FakeSender:
    def __init__(self, formatted_result: list[MessageInfo] | None = None) -> None:
        self.plain: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.formatted: list[tuple[int, str]] = []
        self._formatted_result = (
            formatted_result
            if formatted_result is not None
            else [MessageInfo(chat_id=1, message_id=2, text="x")]
        )

    async def send_plain(self, chat_id: int, text: str) -> MessageInfo:
        self.plain.append((chat_id, text))
        return MessageInfo(chat_id=chat_id, message_id=99, text=text)

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> MessageInfo:
        self.edits.append((chat_id, message_id, text))
        return MessageInfo(chat_id=chat_id, message_id=message_id, text=text)

    async def send_formatted(self, chat_id: int, text: str) -> list[MessageInfo]:
        self.formatted.append((chat_id, text))
        return self._formatted_result


async def test_telegram_chat_view_delegates_to_sender() -> None:
    sender = FakeSender()
    view = TelegramChatView(sender, 42)

    assert view.source_label == "\U0001f4f1 Telegram"
    assert await view.send("resp") is True

    handle = await view.start_progress("\u23f3 OpenCode procesando...")
    await handle.update("tick")
    await handle.finish("\u2705 Completado.")
    await handle.error("\u274c Error: x")

    assert sender.plain == [(42, "\u23f3 OpenCode procesando...")]
    assert sender.edits == [
        (42, 99, "tick"),
        (42, 99, "\u2705 Completado."),
        (42, 99, "\u274c Error: x"),
    ]
    assert sender.formatted == [(42, "resp")]
    assert isinstance(view, ChatView)
    assert isinstance(handle, ProgressHandle)


async def test_telegram_chat_view_send_false_when_nothing_delivered() -> None:
    sender = FakeSender(formatted_result=[])
    view = TelegramChatView(sender, 42)
    assert await view.send("resp") is False


async def test_telegram_progress_handle_noop_when_placeholder_failed() -> None:
    class _FailingSender(FakeSender):
        async def send_plain(self, chat_id: int, text: str) -> None:  # type: ignore[override]
            return None

    sender = _FailingSender()
    view = TelegramChatView(sender, 42)
    handle = await view.start_progress("x")

    await handle.update("tick")
    await handle.finish("done")
    await handle.error("boom")

    assert sender.edits == []


async def test_telegram_progress_placeholder_failure_logs_warning() -> None:
    """F9: a failed placeholder must not be silent.

    The handle is correctly a no-op (the prompt still runs), but the operator
    needs a distinct signal that the *progress surface* is dark — not just
    that one transport message failed.
    """
    import logging

    class _FailingSender(FakeSender):
        async def send_plain(self, chat_id: int, text: str) -> None:  # type: ignore[override]
            return None

    class _Probe(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.DEBUG)
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    logger = logging.getLogger("opencode_bot.services.telegram_chat_view")
    original_level = logger.level
    logger.setLevel(logging.DEBUG)
    probe = _Probe()
    logger.addHandler(probe)
    try:
        sender = _FailingSender()
        view = TelegramChatView(sender, 42)
        await view.start_progress("x")
    finally:
        logger.removeHandler(probe)
        logger.setLevel(original_level)

    assert len(probe.records) == 1, "placeholder failure must be logged once"
    record = probe.records[0]
    assert record.levelno == logging.WARNING
    assert record.__dict__["event"] == "progress_placeholder_unavailable"
    assert record.__dict__["chat_id"] == 42


# ── F3: per-conversation backend isolation ────────────────────────────

class _RecordingBackend:
    """Backend registered in the REAL factory; records cancel() targeting.

    It blocks in ``execute`` until ``release`` is set so two conversations
    are provably in flight at the same time.
    """

    instances: list[_RecordingBackend] = []
    block: bool = False

    def __init__(self, **_kwargs: object) -> None:
        self.cancelled = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not _RecordingBackend.block:
            self.release.set()
        _RecordingBackend.instances.append(self)

    async def execute(
        self, prompt: str, model: str, session_id: str | None,
        agent: str | None = None, workdir: str | None = None,
        source_label: str = "",
        timeout: int | None = None,
    ) -> AIBackendResult:
        self.started.set()
        await self.release.wait()
        return AIBackendResult(stdout=f"ok:{prompt}", returncode=0)

    def cancel(self) -> None:
        self.cancelled = True


async def test_f3_each_conversation_gets_own_backend_and_isolated_cancel() -> None:
    """F3 (a): cancelling one conversation must never touch another's backend.

    Before: the factory cached ONE shared backend whose single
    ``_current_process`` field made ``cancel()`` kill whichever conversation
    happened to own it. Now each conversation owns an independent instance.
    """
    from services.ai_provider_factory import AIProviderFactory

    _RecordingBackend.instances = []
    _RecordingBackend.block = True
    factory = AIProviderFactory(default_provider="opencode")
    factory.register("opencode", _RecordingBackend)
    service = PromptService(session_store=FakeStore(), provider_factory=factory)

    view_a = FakeChatView()
    view_b = FakeChatView()
    task_a = asyncio.create_task(service.execute(101, "alpha", view_a))
    task_b = asyncio.create_task(service.execute(202, "beta", view_b))

    try:
        # Wait until BOTH conversations have their own started backend in flight.
        for _ in range(200):
            if len(_RecordingBackend.instances) == 2 and all(
                b.started.is_set() for b in _RecordingBackend.instances
            ):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("both conversations did not start concurrently")

        backend_a = service._backends[101]
        backend_b = service._backends[202]
        assert backend_a is not backend_b, "each conversation must own its backend instance"
        assert isinstance(backend_a, _RecordingBackend)
        assert isinstance(backend_b, _RecordingBackend)

        # Cancel ONLY conversation 101.
        assert service.cancel(101) is True

        assert backend_a.cancelled is True, "the cancelled conversation's backend must be hit"
        assert backend_b.cancelled is False, "the OTHER conversation's backend must be untouched"

        # Let both finish; A is marked cancelled, B completes normally.
        backend_a.release.set()
        backend_b.release.set()
        await asyncio.gather(task_a, task_b)
    finally:
        _RecordingBackend.block = False

    assert view_a.handles[0].errors == ["\u23f9\ufe0f Cancelado."]
    assert view_a.sent == []
    assert view_b.sent, "the untouched conversation must still deliver"
    assert view_b.handles[0].finished == ["\u2705 Completado."]
    assert service._backends == {}, "per-conversation backends must be released"


async def test_f3_backend_released_after_execution() -> None:
    """The per-conversation backend must not outlive its prompt (no leak)."""
    from services.ai_provider_factory import AIProviderFactory

    _RecordingBackend.instances = []
    factory = AIProviderFactory(default_provider="opencode")
    factory.register("opencode", _RecordingBackend)
    service = PromptService(session_store=FakeStore(), provider_factory=factory)

    view = FakeChatView()
    await service.execute(conversation_id=1, prompt_text="x", view=view)

    assert service._backends == {}, "the backend must be released after the prompt"


# ── F8: partial delivery is a failure + placeholder never sticks ──────

class _PartialDeliveryBot:
    """BotPort fake: delivers the first fragment, fails the second one.

    The long text passed by the tests splits into 2 fragments; fragment 2
    fails BOTH the MDV2 attempt and the plain retry, so ``send_formatted``
    returns 1 of 2.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edits: list[str] = []

    async def send_message(
        self, chat_id: int, text: str, parse_mode: str | None = None,
    ) -> MessageInfo:
        if "parte 2/2" in text:
            raise RuntimeError("transport failure")
        self.sent.append(text)
        return MessageInfo(chat_id=chat_id, message_id=len(self.sent), text=text)

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str,
    ) -> MessageInfo:
        self.edits.append(text)
        return MessageInfo(chat_id=chat_id, message_id=message_id, text=text)

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        return True

    async def get_me(self) -> dict:
        return {"id": 1}


def _two_fragment_text() -> str:
    from config import TELEGRAM_MAX_MESSAGE_LENGTH
    from formatting.markdown import split_message

    text = "x" * (TELEGRAM_MAX_MESSAGE_LENGTH + 50)
    assert len(split_message(text)) == 2
    return text


async def test_f8_partial_delivery_reports_failure_and_logs() -> None:
    """F8 (a): ``send`` means WHOLE delivery; partial emits delivery_partial."""
    import logging

    from services.message_sender import MessageSender

    text = _two_fragment_text()
    bot = _PartialDeliveryBot()
    sender = MessageSender(bot)
    view = TelegramChatView(sender, 7)

    class _Probe(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.DEBUG)
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    logger = logging.getLogger("opencode_bot.services.message_sender")
    probe = _Probe()
    logger.addHandler(probe)
    try:
        ok = await view.send(text)
    finally:
        logger.removeHandler(probe)

    assert ok is False, "a partially delivered response must NOT report success"

    partial = [
        r for r in probe.records
        if r.__dict__.get("event") == "delivery_partial"
    ]
    assert len(partial) == 1, "partial delivery must emit exactly one event"
    assert partial[0].__dict__["sent"] == 1
    assert partial[0].__dict__["total"] == 2
    assert partial[0].levelno == logging.WARNING


async def test_f8_partial_delivery_leaves_placeholder_in_error_state() -> None:
    """F8: an undelivered response must end the placeholder, not leave "⏳".

    Before: ``_deliver_response`` called NEITHER finish NOR error when
    ``response_sent`` was False, so "⏳ OpenCode procesando..." stuck forever.
    """
    from services.message_sender import MessageSender

    text = _two_fragment_text()
    bot = _PartialDeliveryBot()
    sender = MessageSender(bot)
    view = TelegramChatView(sender, 7)

    backend = FakeBackend(AIBackendResult(stdout=text, returncode=0))
    service = PromptService(
        session_store=FakeStore(), provider_factory=FakeFactory(backend),
    )

    await service.execute(conversation_id=7, prompt_text="hi", view=view)

    assert bot.edits, "the placeholder must be edited to a terminal state"
    assert bot.edits[-1].startswith("\u26a0\ufe0f"), bot.edits[-1]
    assert all(
        "OpenCode procesando" not in e for e in bot.edits
    ), "placeholder must not remain in the processing state"
