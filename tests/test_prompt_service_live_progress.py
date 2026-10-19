"""PromptService integration with live-progress backends.

Covers the Phase 2 contract:
  - the elapsed-time ticker runs ONLY for backends without live progress;
  - ``on_progress`` is forwarded only to live backends;
  - the session id is persisted from ``AIBackendResult.session_id`` with no
    stdout regex fallback.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from services.ai_backend import AIBackendResult
from services.chat_view import ConversationId
from services.opencode_server.errors import OpenCodeStreamError
from services.prompt_service import PromptService
from services.session_store import SessionInfo

SID = "ses_live_123"


def _session(real_id: str | None) -> SessionInfo:
    return SessionInfo(
        name="default",
        real_id=real_id,
        title="default",
        created="",
        last_used=None,
        prompt_count=0,
    )


class _Store:
    def __init__(self, active: SessionInfo | None = None) -> None:
        self.active = active
        self.updated: list[tuple[ConversationId, str]] = []
        self.increments = 0

    async def get_model(self, conversation_id: ConversationId) -> str:
        return "test/model"

    async def get_active_session(self, conversation_id: ConversationId) -> SessionInfo | None:
        return self.active

    async def get_chat_setting(
        self, conversation_id: ConversationId, key: str, default: Any = None
    ) -> Any:
        return default

    async def increment_prompt_count(self, conversation_id: ConversationId) -> int:
        self.increments += 1
        return self.increments

    async def update_session_id(self, conversation_id: ConversationId, real_id: str) -> None:
        self.updated.append((conversation_id, real_id))


class _Handle:
    def __init__(self) -> None:
        self.updates: list[str] = []
        self.finished: list[str] = []
        self.errors: list[str] = []

    async def update(self, text: str) -> None:
        self.updates.append(text)

    async def finish(self, text: str) -> None:
        self.finished.append(text)

    async def error(self, text: str) -> None:
        self.errors.append(text)


class _View:
    source_label = "test"

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True

    async def start_progress(self, text: str) -> _Handle:
        self.handle = _Handle()
        return self.handle


class _Backend:
    def __init__(
        self,
        *,
        live: bool,
        result: AIBackendResult,
        delay: float = 0.1,
    ) -> None:
        self.supports_live_progress = live
        self._result = result
        self._delay = delay
        self.received_on_progress: Any = None

    async def execute(
        self,
        prompt: str,
        model: str,
        session_id: str | None,
        agent: str | None = None,
        workdir: str | None = None,
        source_label: str = "",
        timeout: int | None = None,
        on_progress: Any = None,
    ) -> AIBackendResult:
        self.received_on_progress = on_progress
        if on_progress is not None:
            await on_progress("texto en vivo")
        await asyncio.sleep(self._delay)
        return self._result

    def cancel(self) -> None:
        return None


class _Factory:
    def __init__(self, backend: _Backend) -> None:
        self._backend = backend

    def create(self, provider: str | None = None, **kwargs: Any) -> _Backend:
        return self._backend


async def test_live_backend_replaces_the_ticker(monkeypatch) -> None:
    import services.prompt_service as prompt_service

    monkeypatch.setattr(prompt_service, "PROGRESS_UPDATE_INTERVAL", 0.01)
    backend = _Backend(live=True, result=AIBackendResult(stdout="listo"))
    view = _View()
    service = PromptService(
        session_store=_Store(_session(None)), provider_factory=_Factory(backend)
    )

    await service.execute(conversation_id=1, prompt_text="hola", view=view)

    # Only the live callback touched the placeholder: no "(Ns)" ticker.
    assert view.handle.updates == ["texto en vivo"]
    assert backend.received_on_progress is not None


async def test_cli_backend_keeps_the_ticker(monkeypatch) -> None:
    import services.prompt_service as prompt_service

    monkeypatch.setattr(prompt_service, "PROGRESS_UPDATE_INTERVAL", 0.01)
    backend = _Backend(live=False, result=AIBackendResult(stdout="listo"))
    view = _View()
    service = PromptService(
        session_store=_Store(_session(None)), provider_factory=_Factory(backend)
    )

    await service.execute(conversation_id=1, prompt_text="hola", view=view)

    assert backend.received_on_progress is None
    assert view.handle.updates
    assert all("procesando" in text for text in view.handle.updates)


async def test_final_text_is_delivered_and_placeholder_finishes() -> None:
    backend = _Backend(live=True, result=AIBackendResult(stdout="Respuesta final"), delay=0.0)
    view = _View()
    service = PromptService(
        session_store=_Store(_session("ses_existing")), provider_factory=_Factory(backend)
    )

    await service.execute(conversation_id=1, prompt_text="hola", view=view)

    assert view.sent == ["Respuesta final"]
    assert view.handle.finished == ["\u2705 Completado."]


class _FailingBackend(_Backend):
    async def execute(self, *args: Any, **kwargs: Any) -> AIBackendResult:
        raise OpenCodeStreamError("El turno no terminó a tiempo.")


async def test_backend_failure_replaces_the_placeholder_and_reraises() -> None:
    view = _View()
    service = PromptService(
        session_store=_Store(_session(None)),
        provider_factory=_Factory(_FailingBackend(live=True, result=AIBackendResult())),
    )

    with pytest.raises(OpenCodeStreamError):
        await service.execute(conversation_id=1, prompt_text="hola", view=view)

    assert view.handle.errors
    assert view.handle.errors[0].startswith("\u274c Error:")
    assert not view.handle.finished


async def test_session_id_is_not_scraped_from_stdout() -> None:
    store = _Store(_session(None))
    backend = _Backend(
        live=True,
        result=AIBackendResult(stdout=f"texto con {SID} adentro", session_id=None),
        delay=0.0,
    )
    service = PromptService(session_store=store, provider_factory=_Factory(backend))

    await service.execute(conversation_id=1, prompt_text="hola", view=_View())

    assert store.updated == []


async def test_session_id_from_result_is_persisted() -> None:
    store = _Store(_session(None))
    backend = _Backend(
        live=True,
        result=AIBackendResult(stdout="listo", session_id=SID),
        delay=0.0,
    )
    service = PromptService(session_store=store, provider_factory=_Factory(backend))

    await service.execute(conversation_id=1, prompt_text="hola", view=_View())

    assert store.updated == [(1, SID)]
