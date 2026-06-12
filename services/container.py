"""AppContainer — explicit, typed dependency injection container.

All bot dependencies in one dataclass. Immutable after construction.
Injected via PTB's context.application.bot_data["container"].
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class AppContainer:
    """All bot dependencies. Handlers receive this via context."""
    session_store: object   # SessionStore (avoids circular import)
    message_sender: object  # MessageSender
    prompt_service: object  # PromptService
    provider_factory: object   # AIProviderFactory
    bot_port: object        # BotPort
    start_time: float
    allowed_chat_ids: list[int]
    default_model: str
