"""Message presentation atoms: markdown rendering and role styling.

Slice 1 keeps the distinction subtle: the user gets a right-aligned charcoal
bubble, the assistant gets clean full-width markdown, Gemini-style.
"""

from __future__ import annotations

import flet as ft

from flet_app.core.design_system import DS


def build_markdown(value: str) -> ft.Markdown:
    """Render GITHUB_WEB markdown with the design-system text colors."""
    return ft.Markdown(
        value=value,
        selectable=True,
        extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
        md_style_sheet=ft.MarkdownStyleSheet(
            p_text_style=ft.TextStyle(
                size=DS.typography.body_size,
                color=DS.colors.text_primary,
            ),
            code_text_style=ft.TextStyle(
                size=DS.typography.small_size,
                color=DS.colors.accent_soft,
            ),
        ),
    )


def user_message(content: ft.Control) -> ft.Control:
    """Right-aligned surface bubble for the user's turn.

    ``wrap=True`` bounds the bubble to the conversation width, so long
    Markdown wraps instead of overflowing off-screen while short turns keep
    their intrinsic width.
    """
    bubble = ft.Container(
        content=content,
        bgcolor=DS.colors.surface,
        border=ft.Border.all(1, DS.colors.outline),
        border_radius=DS.radii.card,
        padding=ft.Padding.symmetric(
            horizontal=DS.spacing.lg,
            vertical=DS.spacing.md,
        ),
    )
    return ft.Row(
        controls=[bubble],
        wrap=True,
        alignment=ft.MainAxisAlignment.END,
        vertical_alignment=ft.CrossAxisAlignment.START,
    )


def assistant_message(content: ft.Control) -> ft.Control:
    """Plain, full-width assistant turn (no bubble, no chrome)."""
    return ft.Container(
        content=content,
        padding=ft.Padding.symmetric(vertical=DS.spacing.xs),
    )
