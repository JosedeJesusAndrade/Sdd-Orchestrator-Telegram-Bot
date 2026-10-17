"""Composer molecule: the pill input, circular send button and workdir.

The attach and mic controls are visually correct placeholders; voice wiring
arrives in a later slice. The workdir sits *outside* and *below* the pill, in
a deliberately muted tone.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import flet as ft

from flet_app.core.design_system import DS

# Number of trailing path segments kept in the muted workdir caption. Two is
# enough to recognize the project (`…\python\Sdd-Orchestrator-Telegram-Bot`)
# while fitting comfortably on a phone.
_MAX_WORKDIR_SEGMENTS = 2

SendHandler = Callable[
    [ft.Event[ft.TextField] | ft.Event[ft.Container] | None],
    Awaitable[None],
]


@dataclass
class Composer:
    """The composer control plus the pieces the screen needs to drive it."""

    control: ft.Container
    field: ft.TextField
    send_button: ft.Container

    def bind(self, on_send: SendHandler) -> None:
        """Wire submit (Enter) and the circular send button."""
        self.field.on_submit = on_send
        self.send_button.on_click = on_send

    def set_busy(self, busy: bool) -> None:
        """Dim and disable the send action while a prompt is running."""
        self.send_button.disabled = busy
        self.send_button.opacity = 0.55 if busy else 1.0


def shorten_workdir(workdir: str) -> str:
    """Keep only the trailing path segments, the part users care about.

    Flet's ``TextOverflow.ELLIPSIS`` truncates the *end* of the string, which
    hides the project name on a long Windows path. Truncating the *front*
    instead guarantees the active directory stays readable on a phone.
    """
    parts = Path(workdir).parts
    if len(parts) <= _MAX_WORKDIR_SEGMENTS + 1:
        return workdir
    return f"…{os.sep}{os.sep.join(parts[-_MAX_WORKDIR_SEGMENTS:])}"


def build_composer(*, workdir: str) -> Composer:
    """Build the pill composer and the muted workdir caption below it."""
    field = ft.TextField(
        hint_text="Pregunta, escribe o pide algo…",
        expand=True,
        filled=False,
        border=ft.NoInputBorder(),
        bgcolor=ft.Colors.TRANSPARENT,
        color=DS.colors.text_primary,
        cursor_color=DS.colors.accent,
        text_size=DS.typography.body_size,
        hint_style=ft.TextStyle(
            size=DS.typography.body_size,
            color=DS.colors.text_secondary,
        ),
        content_padding=ft.Padding.symmetric(
            horizontal=DS.spacing.xs,
            vertical=DS.spacing.md,
        ),
        multiline=False,
    )

    send_button = ft.Container(
        width=40,
        height=40,
        border_radius=20,
        bgcolor=DS.colors.accent,
        alignment=ft.Alignment.CENTER,
        tooltip="Enviar",
        ink=True,
        shadow=ft.BoxShadow(
            blur_radius=18,
            color=DS.colors.alpha(DS.colors.accent, 0.4),
        ),
        content=ft.Icon(ft.Icons.ARROW_UPWARD, size=20, color=DS.colors.white),
    )

    pill = ft.Container(
        bgcolor=DS.colors.surface,
        border=ft.Border.all(1, DS.colors.outline),
        border_radius=DS.radii.pill,
        padding=ft.Padding.symmetric(horizontal=DS.spacing.sm, vertical=DS.spacing.xs),
        content=ft.Row(
            controls=[
                ft.IconButton(
                    icon=ft.Icons.ADD,
                    icon_color=DS.colors.text_secondary,
                    icon_size=22,
                    tooltip="Adjuntar (próximamente)",
                ),
                field,
                ft.IconButton(
                    icon=ft.Icons.MIC,
                    icon_color=DS.colors.text_secondary,
                    icon_size=22,
                    tooltip="Voz (próximamente)",
                ),
                send_button,
            ],
            spacing=DS.spacing.xs,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )

    workdir_text = ft.Text(
        value=shorten_workdir(workdir),
        size=DS.typography.muted_size,
        color=DS.colors.text_muted,
        text_align=ft.TextAlign.CENTER,
        no_wrap=True,
        overflow=ft.TextOverflow.ELLIPSIS,
    )
    workdir_caption = ft.Container(
        content=workdir_text,
        alignment=ft.Alignment.CENTER,
        padding=ft.Padding.only(top=DS.spacing.sm),
        tooltip=workdir,
    )

    control = ft.Container(
        content=ft.Column(
            controls=[pill, workdir_caption],
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        ),
    )
    return Composer(control=control, field=field, send_button=send_button)
