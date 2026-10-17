"""Flet tracer bullet: the thinnest end-to-end GUI over the existing core.

Run (desktop):  .venv\\Scripts\\flet.exe run flet_app/main.py
Android (QR):   .venv\\Scripts\\flet.exe run --android flet_app/main.py

The UI is deliberately minimal: conversations, one prompt field, one button.
The prompt travels through the SAME ``build_prompt_stack()`` composition root
and ``PromptService.execute`` the Telegram bot uses; only the ``ChatView``
implementation differs. Everything is async on Flet's single event loop —
never ``page.run_thread`` (the core is loop-bound and contextvar-aware).
"""

from __future__ import annotations

import sys
from pathlib import Path

# ``flet run`` may execute this file with flet_app/ as sys.path[0]; make the
# repository root importable so ``services.*`` resolves.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import flet as ft  # noqa: E402

from flet_app.flet_chat_view import FletChatView  # noqa: E402
from services.composition import build_prompt_stack  # noqa: E402
from services.frontend_gate import AllowAllGate  # noqa: E402
from services.prompt_service import PromptAlreadyRunningError  # noqa: E402

CONVERSATION_ID = "flet"


def main(page: ft.Page) -> None:
    page.title = "SDD Orchestrator — Flet"
    page.padding = 16

    core = build_prompt_stack()
    gate = AllowAllGate()

    conversation = ft.Column(
        controls=[],
        spacing=12,
        scroll=ft.ScrollMode.AUTO,
        auto_scroll=True,
        expand=True,
    )
    view = FletChatView(page, conversation)

    prompt_field = ft.TextField(
        hint_text="Escribe un prompt…",
        autofocus=True,
        expand=True,
        multiline=True,
        min_lines=1,
        max_lines=5,
    )
    send_button = ft.FilledButton("Enviar", icon=ft.Icons.SEND)

    async def on_send(
        event: ft.Event[ft.Button] | ft.Event[ft.TextField] | None = None,
    ) -> None:
        text = (prompt_field.value or "").strip()
        if not text:
            return

        if not gate.is_allowed(CONVERSATION_ID):
            await view.send("⛔ No autorizado.")
            return

        prompt_field.value = ""
        send_button.disabled = True
        page.update()

        await view.send(f"**Tú:**\n\n{text}")

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
            send_button.disabled = False
            page.update()

    send_button.on_click = on_send
    prompt_field.on_submit = on_send

    page.add(
        ft.Column(
            controls=[
                ft.Text("SDD Orchestrator", size=20, weight=ft.FontWeight.BOLD),
                ft.Text(
                    "Flet tracer bullet — mismo core que Telegram",
                    size=12,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
                ft.Divider(),
                conversation,
                ft.Row(controls=[prompt_field, send_button], spacing=8),
            ],
            expand=True,
            spacing=12,
        )
    )


if __name__ == "__main__":
    ft.run(main)
