"""Flet GUI entry point — Slice 1: design system + polished chat screen.

Run (desktop):  .venv\\Scripts\\flet.exe run flet_app/main.py
Android (QR):   .venv\\Scripts\\flet.exe run --android flet_app/main.py

The UI is the Gemini-style shell from ``flet_app/screens`` while the prompt
travels through the SAME ``build_prompt_stack()`` composition root and
``PromptService.execute`` the Telegram bot uses; only the ``ChatView``
implementation differs. Everything is async on Flet's single event loop —
never ``page.run_thread`` (the core is loop-bound and contextvar-aware).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# ``flet run`` may execute this file with flet_app/ as sys.path[0]; make the
# repository root importable so ``services.*`` resolves.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import flet as ft  # noqa: E402

from config import BASE_DIR, DEFAULT_MODEL, setup_logger  # noqa: E402

# Entry point: attach console + file handlers before anything logs. Importing
# config/library modules has no logging side effects by design.
setup_logger()

from flet_app.core.design_system import DS  # noqa: E402
from flet_app.core.navigation import ScreenHost  # noqa: E402
from flet_app.flet_chat_view import FletChatView, render_history  # noqa: E402
from flet_app.screens.chat_screen import build_chat_screen  # noqa: E402
from flet_app.screens.conversations_screen import ConversationsScreen  # noqa: E402
from services.agent_sessions import ConversationSummary, Message  # noqa: E402
from services.composition import build_prompt_stack  # noqa: E402
from services.frontend_gate import AllowAllGate  # noqa: E402
from services.prompt_service import PromptAlreadyRunningError  # noqa: E402

CONVERSATION_ID = "flet"

logger = logging.getLogger(__name__)


async def main(page: ft.Page) -> None:
    page.title = "SDD Orchestrator"
    page.padding = 0
    page.bgcolor = DS.colors.background
    page.theme_mode = ft.ThemeMode.DARK

    core = build_prompt_stack()
    gate = AllowAllGate()

    model = DEFAULT_MODEL
    workdir = str(BASE_DIR)
    try:
        settings = await core.settings_service.get(CONVERSATION_ID)
        model = settings.model
        workdir = str(settings.workdir)

        # The Flet GUI is the front-end of THIS repository. ``OPENCODE_WORKDIR``
        # (.env) belongs to the Telegram bot and may point at a parent folder
        # (e.g. ``...\mono\python``), which is why the caption looked "cut".
        # When the Flet conversation has no explicit workdir, bind it to the
        # project root so the caption and the prompts agree on the relevant
        # directory. Telegram conversations are untouched (separate ids).
        explicit = await core.session_store.get_all_chat_settings(CONVERSATION_ID)
        if "workdir" not in explicit:
            workdir = str(BASE_DIR)
            await core.settings_service.set(CONVERSATION_ID, "workdir", workdir)
    except Exception:
        logger.exception("No se pudieron leer los settings; usando defaults")

    def open_menu() -> None:
        page.run_task(show_conversations)

    chat = build_chat_screen(
        model=model,
        workdir=workdir,
        on_menu=open_menu,
    )
    view = FletChatView(
        page,
        chat.conversation,
        on_first_message=chat.reveal_conversation,
    )

    host = ScreenHost(page, chat.control)

    def go_back() -> None:
        page.run_task(host.show, chat.control)

    def on_open(
        summary: ConversationSummary,
        messages: list[Message],
        active_name: str,
    ) -> None:
        logger.info("Conversación abierta: %s (%s)", summary.title, active_name)
        render_history(chat.conversation, messages)
        chat.reveal_conversation()
        page.run_task(host.show, chat.control)

    conversations = ConversationsScreen(
        page,
        core.conversations_service,
        CONVERSATION_ID,
        on_back=go_back,
        on_open=on_open,
    )

    async def show_conversations() -> None:
        await host.show(conversations.control)
        await conversations.load()

    async def on_send(
        event: ft.Event[ft.TextField] | ft.Event[ft.Container] | None = None,
    ) -> None:
        text = (chat.composer.field.value or "").strip()
        if not text:
            return

        if not gate.is_allowed(CONVERSATION_ID):
            await view.send("⛔ No autorizado.")
            return

        chat.composer.field.value = ""
        chat.composer.set_busy(True)
        page.update()

        await view.send(text, role="user")

        try:
            await core.prompt_service.execute(
                conversation_id=CONVERSATION_ID,
                prompt_text=text,
                view=view,
            )
        except PromptAlreadyRunningError:
            await view.send("⏳ Ya hay un prompt en ejecución.")
        except Exception as exc:
            await view.send(f"❌ Error inesperado: `{exc}`")
        finally:
            chat.composer.set_busy(False)
            page.update()

    chat.composer.bind(on_send)
    page.add(host.control)


if __name__ == "__main__":
    ft.run(main)
