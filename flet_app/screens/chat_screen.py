"""Chat screen: assembles the top bar, greeting, conversation and composer.

Composition follows Gemini: chrome at the top, a centered greeting in the
empty state, a scrolling conversation once messages exist, and a floating
pill composer pinned to the bottom with the workdir muted underneath.
"""

from __future__ import annotations

from dataclasses import dataclass

import flet as ft

from flet_app.components.composer import Composer, build_composer
from flet_app.components.greeting import build_greeting
from flet_app.components.top_bar import TopBar, build_top_bar
from flet_app.core.design_system import DS


@dataclass
class ChatScreen:
    """The assembled screen plus handles the entry point needs to drive it."""

    control: ft.Container
    top_bar: TopBar
    composer: Composer
    conversation: ft.Column
    greeting: ft.Container
    conversation_holder: ft.Container

    def set_model(self, model: str) -> None:
        self.top_bar.set_model(model)

    def reveal_conversation(self) -> None:
        """Swap the greeting for the conversation on the first message."""
        self.greeting.visible = False
        self.conversation_holder.visible = True


def build_chat_screen(*, model: str, workdir: str) -> ChatScreen:
    """Build the Slice 1 chat screen."""
    top_bar = build_top_bar(model)
    greeting = build_greeting()
    composer = build_composer(workdir=workdir)

    conversation = ft.Column(
        controls=[],
        spacing=DS.spacing.md,
        scroll=ft.ScrollMode.AUTO,
        auto_scroll=True,
        expand=True,
    )
    conversation_holder = ft.Container(
        content=conversation,
        expand=True,
        visible=False,
        padding=ft.Padding.only(
            left=DS.spacing.xl,
            right=DS.spacing.xl,
            top=DS.spacing.sm,
        ),
    )

    composer_holder = ft.Container(
        content=ft.ResponsiveRow(
            controls=[
                ft.Container(
                    content=composer.control,
                    col={"sm": 12, "md": 10, "lg": 8},
                )
            ],
            alignment=ft.MainAxisAlignment.CENTER,
        ),
        padding=ft.Padding.only(
            left=DS.spacing.xl,
            right=DS.spacing.xl,
            top=DS.spacing.sm,
            bottom=DS.spacing.md,
        ),
    )

    body = ft.Column(
        controls=[top_bar.control, greeting, conversation_holder, composer_holder],
        expand=True,
        spacing=0,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )

    control = ft.Container(
        expand=True,
        gradient=DS.gradients.background(),
        content=ft.Stack(
            expand=True,
            fit=ft.StackFit.EXPAND,
            controls=[
                ft.Container(
                    width=520,
                    height=520,
                    top=-200,
                    right=-180,
                    border_radius=260,
                    gradient=DS.gradients.lavender_glow(),
                    ignore_interactions=True,
                ),
                ft.Container(
                    width=420,
                    height=420,
                    bottom=-220,
                    left=-140,
                    border_radius=210,
                    gradient=DS.gradients.blue_glow(),
                    ignore_interactions=True,
                ),
                body,
            ],
        ),
    )

    return ChatScreen(
        control=control,
        top_bar=top_bar,
        composer=composer,
        conversation=conversation,
        greeting=greeting,
        conversation_holder=conversation_holder,
    )
