"""Top bar molecule: menu, active model, new-chat and avatar placeholders.

The controls are visually complete but inert in Slice 1 — session navigation,
the model picker and the profile menu are wired in later slices.
"""

from __future__ import annotations

from dataclasses import dataclass

import flet as ft

from flet_app.core.design_system import DS

_MODEL_DISPLAY_NAMES: dict[str, str] = {
    "deepseek/deepseek-v4-pro": "DeepSeek V4 Pro",
    "deepseek/deepseek-flash": "DeepSeek Flash",
}


def model_display_name(model: str) -> str:
    """Turn a provider model id into a short, human label."""
    known = _MODEL_DISPLAY_NAMES.get(model)
    if known is not None:
        return known
    tail = model.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").strip()
    return tail.title() if tail else model


@dataclass
class TopBar:
    """The assembled bar plus the mutable model label."""

    control: ft.Container
    model_text: ft.Text

    def set_model(self, model: str) -> None:
        self.model_text.value = model_display_name(model)


def _placeholder_handler(_: ft.Event[ft.Container]) -> None:
    """Inert click target for controls that arrive in later slices."""


def build_top_bar(model: str) -> TopBar:
    """Build the Gemini-like top bar for the active ``model``."""
    model_text = ft.Text(
        value=model_display_name(model),
        size=DS.typography.title_size,
        weight=DS.typography.title_weight,
        color=DS.colors.text_primary,
    )

    model_selector = ft.Container(
        content=ft.Row(
            controls=[
                model_text,
                ft.Icon(
                    ft.Icons.EXPAND_MORE,
                    size=18,
                    color=DS.colors.text_secondary,
                ),
            ],
            spacing=DS.spacing.xs,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        padding=ft.Padding.symmetric(horizontal=DS.spacing.sm, vertical=DS.spacing.xs),
        border_radius=DS.radii.small,
        ink=True,
        tooltip="Selector de modelo (próximamente)",
        on_click=_placeholder_handler,
    )

    avatar = ft.Container(
        width=36,
        height=36,
        border_radius=18,
        gradient=ft.LinearGradient(
            begin=ft.Alignment.TOP_LEFT,
            end=ft.Alignment.BOTTOM_RIGHT,
            colors=[DS.colors.accent_soft, DS.colors.accent],
        ),
        alignment=ft.Alignment.CENTER,
        tooltip="Perfil (próximamente)",
        content=ft.Icon(ft.Icons.PERSON, size=18, color=DS.colors.white),
    )

    row = ft.Row(
        controls=[
            ft.IconButton(
                icon=ft.Icons.MENU,
                icon_color=DS.colors.text_primary,
                icon_size=22,
                tooltip="Sesiones (próximamente)",
            ),
            model_selector,
            ft.Container(expand=True),
            ft.IconButton(
                icon=ft.Icons.EDIT,
                icon_color=DS.colors.text_primary,
                icon_size=20,
                tooltip="Nuevo chat (próximamente)",
            ),
            avatar,
        ],
        spacing=DS.spacing.sm,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    # Slightly more top air than the Slice 1 symmetric ``md`` (12 -> 13, ~10%)
    # so the bar clears the Android status bar even without an inset.
    vertical = DS.spacing.md + DS.spacing.xs // 4
    control = ft.Container(
        content=row,
        padding=ft.Padding.only(
            left=DS.spacing.xl,
            right=DS.spacing.xl,
            top=vertical,
            bottom=DS.spacing.md,
        ),
    )
    return TopBar(control=control, model_text=model_text)
