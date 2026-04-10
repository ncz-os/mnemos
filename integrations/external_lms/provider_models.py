"""External provider model registry for MNEMOS.

This module provides a lightweight public-safe abstraction for provider model
listings. In production, deployments can extend this with real provider API
queries and caching. For open-source bootstrap and testability, it exposes a
small in-memory registry with refresh helpers.
"""

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class ProviderModels:
    providers: Dict[str, List[str]] = field(default_factory=lambda: {
        "openai": ["gpt-4.1", "gpt-4o-mini"],
        "google": ["gemini-2.5-pro", "gemini-2.5-flash"],
        "groq": ["llama-3.3-70b", "mixtral-8x7b"],
        "ollama": ["llama3.2", "nomic-embed-text"],
    })

    def list_providers(self) -> List[str]:
        return sorted(self.providers.keys())

    def get_models(self, provider: str) -> List[str]:
        return list(self.providers.get(provider, []))

    def refresh(self) -> Dict[str, List[str]]:
        return self.providers


def get_provider_models() -> ProviderModels:
    return ProviderModels()


def refresh_provider_models() -> Dict[str, List[str]]:
    return ProviderModels().refresh()
