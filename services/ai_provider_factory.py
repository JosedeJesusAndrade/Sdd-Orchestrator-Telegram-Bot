"""AIProviderFactory — resolves AI backends per provider name."""
from __future__ import annotations
from typing import Any

class AIProviderFactory:
    def __init__(self, default_provider: str = "opencode"):
        self._providers: dict[str, type] = {}
        self._instances: dict[str, Any] = {}
        self._default = default_provider
    
    def register(self, name: str, backend_cls: type) -> None:
        self._providers[name] = backend_cls
    
    def get(self, provider: str | None = None, **kwargs) -> Any:
        name = provider or self._default
        if name not in self._providers:
            raise ValueError(f"Unknown provider '{name}'. Available: {list(self._providers)}")
        if name not in self._instances:
            self._instances[name] = self._providers[name](**kwargs)
        return self._instances[name]
    
    def list_providers(self) -> list[str]:
        return list(self._providers.keys())
    
    def reset(self) -> None:
        self._instances.clear()
