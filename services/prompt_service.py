"""PromptService: execute OpenCode prompts, manage output, track state.
  
Architecture rationale:
  The old _process_prompt() was a 273-line monolithic function inside
  handlers/messages.py. It handled:
    - Session sync (JSON load/save, in-memory dicts)
    - Model resolution (3 fallback sources)
    - Subprocess execution (Popen, timeout, cancel)
    - Response formatting (filter, clean, escape, split)
    - Message delivery (send, edit, fallback)
    - Session ID capture

  This violated SRP (Single Responsibility Principle) and made the code
  untestable — you couldn't test the execution logic without a Telegram bot.

  PromptService solves this by:
    - Delegating session I/O to SessionStore
    - Delegating message delivery to MessageSender
    - Focusing ONLY on orchestration: build the command, run it,
      route the output to the right delivery method
    - Encapsulating process state (running tasks, cancel flags)

  Why a class?
    - Stateful: tracks running tasks per chat_id
    - Injectable: SessionStore + MessageSender + AIProviderFactory in constructor
    - Testable: mock the backend and verify behavior

  Week 4: AIProviderFactory replaces direct AIBackend. Per-chat settings
  (provider, timeout, workdir) read from SessionStore. Factory resolves
  the correct backend per provider.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from config import (
    DEFAULT_MODEL, DEFAULT_SESSION_NAME,
    OPENCODE_TIMEOUT, OPENCODE_WORKDIR,
    logger,
)

if TYPE_CHECKING:
    from services.session_store import SessionStore
    from services.message_sender import MessageSender

logger = logging.getLogger(__name__)


class PromptAlreadyRunningError(Exception):
    """Raised when a prompt is already running for a chat."""


class PromptService:
    """Orchestrate the lifecycle of an OpenCode prompt execution.

    Responsibilities:
      1. Check if a prompt is already running (no double-execution)
      2. Resolve model + session from SessionStore
      3. Build and execute the opencode CLI command
      4. Capture the session ID on first run
      5. Format and deliver the response via MessageSender
      6. Track prompt counts
      7. Support cancellation
    """

    def __init__(
        self,
        session_store: SessionStore,
        message_sender: MessageSender,
        provider_factory: object,
    ) -> None:
        self._store = session_store
        self._sender = message_sender
        self._factory = provider_factory

        # Per-chat running tasks
        self._running: dict[int, asyncio.Task] = {}
        # Per-chat cancel flags (set by cancel() before process terminates)
        self._cancel: set[int] = set()

    # ── Public API ──────────────────────────────────────────────────

    def is_running(self, chat_id: int) -> bool:
        """Check if a prompt is currently executing for this chat."""
        return chat_id in self._running

    def cancel(self, chat_id: int) -> bool:
        """Request cancellation of a running prompt.

        Returns True if there was a prompt to cancel, False otherwise.
        Cancellation is asynchronous — the prompt may take a moment
        to actually stop after this returns.
        """
        if chat_id not in self._running:
            return False
        self._cancel.add(chat_id)
        for backend in self._factory._instances.values():
            try:
                backend.cancel()
            except Exception:
                pass
        return True

    async def execute(
        self,
        chat_id: int,
        prompt_text: str,
        update_for_logging=None,
    ) -> str:
        """Execute a prompt for the given chat.

        This is the main entry point. It handles the full lifecycle:
        session sync → execution → delivery → cleanup.

        Args:
            chat_id: Telegram chat ID.
            prompt_text: The user's prompt text.
            update_for_logging: Optional Update object for logging.

        Returns:
            The raw stdout output from OpenCode.

        Raises:
            PromptAlreadyRunningError: if a prompt is already running.
        """
        if chat_id in self._running:
            raise PromptAlreadyRunningError(
                f"Chat {chat_id} already has a running prompt"
            )

        try:
            task = asyncio.current_task()
            if task is not None:
                self._running[chat_id] = task

            # 1. Resolve session and model
            session = await self._store.get_active_session(chat_id)
            model = await self._store.get_model(chat_id)

            if update_for_logging is not None:
                self._log_prompt(chat_id, prompt_text, model)

            # 2. Send "Processing..." indicator
            proc_msg = await self._sender.send_plain(
                chat_id, "\u23f3 OpenCode procesando..."
            )

            # 3. Execute OpenCode via AI backend
            result = await self._execute_prompt(
                chat_id, prompt_text, session, model,
            )

            # 4. Deliver response
            await self._deliver_response(chat_id, result, proc_msg, session)

            return result["stdout"]

        finally:
            self._running.pop(chat_id, None)
            self._cancel.discard(chat_id)

    # ── Internal: AI backend execution ──────────────────────────────

    async def _execute_prompt(
        self,
        chat_id: int,
        prompt: str,
        session,
        model: str,
    ) -> dict:
        """Execute prompt via the AI provider factory.

        Reads per-chat settings (provider, timeout, workdir) from
        SessionStore. Resolves the correct AI backend via the factory.

        Returns a dict with keys: stdout, stderr, returncode, cancelled.
        """
        provider = await self._store.get_chat_setting(chat_id, "provider", "opencode")
        timeout_val = await self._store.get_chat_setting(chat_id, "timeout", OPENCODE_TIMEOUT)
        workdir = await self._store.get_chat_setting(chat_id, "workdir", OPENCODE_WORKDIR)

        backend = self._factory.get(provider)
        result = await backend.execute(
            prompt=prompt,
            model=model,
            session_id=session.real_id if session else None,
            workdir=str(workdir),
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "cancelled": chat_id in self._cancel or result.cancelled,
        }

    # ── Internal: response delivery ─────────────────────────────────

    async def _deliver_response(
        self,
        chat_id: int,
        result: dict,
        proc_msg,
        session,
    ) -> None:
        """Format and send the OpenCode response, or show an error."""
        from formatting.markdown import clean_opencode_output, _assemble_response

        # Handle cancellation
        if result["cancelled"]:
            await self._sender.edit_message(
                chat_id, proc_msg.message_id, "\u23f9\ufe0f Cancelado."
            )
            return

        # Handle hard errors (non-zero exit + empty stdout, or errors via stderr)
        has_error = (
            result["returncode"] != 0
            or (result["stderr"] and not result["stdout"].strip())
        )
        if has_error:
            error_text = result["stderr"] or result["stdout"] or "Unknown error"
            await self._sender.edit_message(
                chat_id, proc_msg.message_id,
                f"\u274c Error: {error_text[:500]}",
            )
            return

        # Process and format the response
        cleaned = clean_opencode_output(result["stdout"])
        response = _assemble_response(cleaned, result["stderr"])

        if not response.strip():
            if result["returncode"] != 0:
                response = f"\u274c Error (c\u00f3digo {result['returncode']}): sin output."
            else:
                response = "\u2705 Completado (sin output)."

        # Prepend new-session header for brand-new sessions
        if session is not None and not session.real_id and session.name:
            response = (
                f"\U0001f195 Nueva sesión '{session.name}' iniciada\n\n"
                + response
            )

        # Track whether we actually delivered content
        is_success = result["returncode"] == 0
        response_sent = False

        # Send formatted response
        sent = await self._sender.send_formatted(chat_id, response)
        if sent:
            response_sent = True

        # Edit "Processing..." to "Completed" only if successful
        if response_sent and is_success:
            try:
                await self._sender.edit_message(
                    chat_id, proc_msg.message_id, "\u2705 Completado."
                )
            except Exception:
                pass

        # Capture session ID for new sessions
        await self._capture_session_id(chat_id, result["stdout"], session)

        # Increment prompt count
        try:
            await self._store.increment_prompt_count(chat_id)
        except Exception as e:
            logger.warning("Failed to increment prompt count: %s", e)

    async def _capture_session_id(
        self,
        chat_id: int,
        stdout: str,
        session,
    ) -> None:
        """Extract and persist the real OpenCode session ID from stdout.

        After the first prompt in a new session, OpenCode assigns a
        real session ID (e.g., ses_AbCdEf123). We capture this so
        subsequent prompts use --continue.
        """
        match = re.search(r'(ses_[a-zA-Z0-9]+)', stdout)
        if not match:
            return
        real_id = match.group(1)

        active = await self._store.get_active_session(chat_id)
        if active is not None and not active.real_id:
            await self._store.update_session_id(chat_id, real_id)
            logger.info("Captured session ID %s for chat %s", real_id, chat_id)

    # ── Internal: logging ───────────────────────────────────────────

    def _log_prompt(self, chat_id: int, prompt: str, model: str) -> None:
        """Log the incoming prompt (with credential redaction)."""
        from utils.logging import mask_chat_id

        truncated = prompt[:100] + "..." if len(prompt) > 100 else prompt
        safe_prompt = truncated
        for pattern in ['sk-', 'Bearer ', '-----BEGIN', 'token=', 'secret=']:
            if pattern.lower() in safe_prompt.lower():
                safe_prompt = "[REDACTED - possible credential]"
                break
        logger.info(
            "Request from %s | prompt=%r | len=%d | model=%s",
            mask_chat_id(chat_id),
            safe_prompt,
            len(prompt),
            model,
        )
