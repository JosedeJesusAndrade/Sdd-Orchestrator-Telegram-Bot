"""AppContainer — explicit, typed dependency injection container.

All bot dependencies in one dataclass. Immutable after construction.
Injected via PTB's context.application.bot_data["container"].
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.ai_provider_factory import AIProviderFactory
    from services.bot_port import BotPort
    from services.message_sender import MessageSender
    from services.prompt_service import PromptService
    from services.session_store import SessionStore


@dataclass
class AppContainer:
    """All bot dependencies. Handlers receive this via context."""
    session_store: SessionStore
    message_sender: MessageSender
    prompt_service: PromptService
    provider_factory: AIProviderFactory
    bot_port: BotPort
    start_time: float
    allowed_chat_ids: list[int]
    default_model: str
