"""AIBackend Protocol — structural subtyping for AI execution backends.

Any object with an async execute() method matching this signature
IS an AIBackend. No inheritance needed. Supports:
  - OpenCodeCLIBackend (subprocess)
  - ClaudeAPIBackend (HTTP)
  - OllamaBackend (local HTTP)
  - MockAIBackend (testing)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class AIBackendResult:
    """Unified result from any AI backend."""

    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    cancelled: bool = False
    timed_out: bool = False
    session_id: str | None = None


@runtime_checkable
class AIBackend(Protocol):
    """Protocol for AI execution backends."""

    #: True when ``execute`` can push incremental text via ``on_progress``.
    #: PromptService keeps its elapsed-time ticker only when this is False.
    supports_live_progress: bool

    async def execute(
        self,
        prompt: str,
        model: str,
        session_id: str | None,
        agent: str | None = None,
        workdir: str | None = None,
        source_label: str = "",
        timeout: float | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> AIBackendResult:
        """Execute a prompt and return the result.

        Args:
            timeout: Per-call timeout in seconds. ``None`` means "use the
                backend's configured default" (e.g. the constructor timeout).
            on_progress: Optional async callback for incremental output.
                Backends that do not stream (the CLI) accept and ignore it.
        """
        ...

    def cancel(self) -> None:
        """Cancel the current execution (if any)."""
        ...
