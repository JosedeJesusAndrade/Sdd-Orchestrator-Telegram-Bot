"""AIBackend Protocol — structural subtyping for AI execution backends.

Any object with an async execute() method matching this signature
IS an AIBackend. No inheritance needed. Supports:
  - OpenCodeCLIBackend (subprocess)
  - ClaudeAPIBackend (HTTP)
  - OllamaBackend (local HTTP)
  - MockAIBackend (testing)
"""
from __future__ import annotations
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


@runtime_checkable
class AIBackend(Protocol):
    """Protocol for AI execution backends."""
    
    async def execute(
        self,
        prompt: str,
        model: str,
        session_id: str | None,
        workdir: str,
    ) -> AIBackendResult:
        """Execute a prompt and return the result."""
        ...
    
    def cancel(self) -> None:
        """Cancel the current execution (if any)."""
        ...
