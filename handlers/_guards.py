"""Typed accessors for PTB ``Update`` fields that are Optional in the types.

python-telegram-bot types ``Update.effective_chat``, ``Update.message`` and
``Message.text`` as ``X | None`` even though the filters that route an
update to a handler (TEXT/VOICE/COMMAND) guarantee those fields are
present. This module performs that narrowing in ONE place.

The ``assert`` is a runtime no-op that satisfies the type checker and
documents the invariant: if it ever fires, the handler was registered
without a filter that guarantees the field — a programming error, not a
recoverable runtime condition. If a caller can genuinely receive ``None``
(no such filter), do NOT use these helpers; handle ``None`` explicitly
with a real early-return.
"""

from __future__ import annotations

from telegram import Chat, Message, Update


def require_chat(update: Update) -> Chat:
    """Return ``update.effective_chat``, asserting it is present."""
    chat = update.effective_chat
    assert chat is not None, "update without effective_chat"
    return chat


def require_message(update: Update) -> Message:
    """Return ``update.message``, asserting it is present.

    Uses ``update.message`` (the field every existing call site used) rather
    than ``update.effective_message`` so semantics are byte-for-byte
    unchanged; for the registered TEXT/VOICE/COMMAND filters the two are
    equivalent.
    """
    message = update.message
    assert message is not None, "update without message"
    return message


def require_text(message: Message) -> str:
    """Return ``message.text``, asserting it is present.

    Only valid for handlers behind a filter that guarantees text
    (``filters.TEXT``). ``Message.text`` is ``str | None`` in PTB.
    """
    text = message.text
    assert text is not None, "message without text"
    return text
