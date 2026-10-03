"""AIProviderFactory — registers AI backend providers and builds instances.

A factory, not a cache. ``create()`` ALWAYS returns a NEW backend instance.

Why (F3): the previous implementation eagerly cached ONE shared instance per
provider in ``_instances``. That singleton held a single mutable
``_current_process``, so a cancel/timeout in conversation A killed whichever
process the shared field happened to point at — possibly conversation B's.
Removing the cache removes the shared mutable state; each conversation now
owns its own backend instance (created by ``PromptService``, which owns the
per-conversation lifecycle). Multiple providers stay possible: a provider is
a registered class plus its constructor defaults.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from services.ai_backend import AIBackend


class ProviderFactoryPort(Protocol):
    """Minimal structural contract ``PromptService`` needs from a factory."""

    def create(self, provider: str | None = None, **kwargs: Any) -> AIBackend:
        ...


class AIProviderFactory:
    def __init__(self, default_provider: str = "opencode"):
        self._providers: dict[str, type] = {}
        self._default_kwargs: dict[str, dict[str, Any]] = {}
        self._default = default_provider

    def register(
        self, name: str, backend_cls: type, **default_kwargs: Any
    ) -> None:
        """Register a provider class and the kwargs used to build it.

        Defaults are merged with (and overridden by) any kwargs passed to
        ``create()``. Callers that want per-call settings can pass them at
        create time; global config (e.g. CLI path) is captured here once.
        """
        self._providers[name] = backend_cls
        self._default_kwargs[name] = dict(default_kwargs)

    def create(self, provider: str | None = None, **kwargs: Any) -> Any:
        """Build a fresh backend instance for ``provider``.

        Never caches — every call yields an independent instance so two
        conversations can never share mutable process state.
        """
        name = provider or self._default
        if name not in self._providers:
            raise ValueError(
                f"Unknown provider '{name}'. Available: {list(self._providers)}"
            )
        merged = {**self._default_kwargs.get(name, {}), **kwargs}
        return self._providers[name](**merged)

    def list_providers(self) -> list[str]:
        return list(self._providers.keys())
