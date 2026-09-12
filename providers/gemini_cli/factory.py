"""Factory classes for the Gemini CLI Provider (TASK-008).

The Application layer never constructs GeminiCliProvider /
GeminiCliResource directly; it registers this package's ProviderDefinition
with the ProviderRegistry, which creates both on demand.
One OAuth credential (account) => one GeminiCliResource.
"""
from __future__ import annotations

from typing import Any, List

from providers.gemini_cli.provider import GeminiCliProvider
from providers.gemini_cli.resource import GeminiCliResource


class GeminiCliProviderFactory:
    """Creates a fresh GeminiCliProvider instance."""

    def create_provider(
        self,
        provider_id: str,
        config: Any = None,
    ) -> GeminiCliProvider:
        models = None
        if config and isinstance(config, dict):
            models = config.get("models")
        return GeminiCliProvider(models=models)


class GeminiCliResourceFactory:
    """Builds GeminiCliResource objects from config dicts."""

    def create_resources(
        self,
        provider_id: str,
        config: List[dict],
    ) -> List[GeminiCliResource]:
        resources = []
        for item in config:
            payload = dict(item)
            payload.setdefault("provider", provider_id)
            resources.append(GeminiCliResource.model_validate(payload))
        return resources
