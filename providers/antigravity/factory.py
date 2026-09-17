from __future__ import annotations

from typing import Any, List

from .provider import AntigravityProvider
from .resource import AntigravityResource


class AntigravityProviderFactory:
    def create_provider(self, provider_id: str, config: Any = None) -> AntigravityProvider:
        return AntigravityProvider(AntigravityResource(id=provider_id))


class AntigravityResourceFactory:
    def create_resources(self, provider_id: str, config: list) -> list:
        resources = []
        for item in config:
            payload = dict(item)
            payload.setdefault('provider', provider_id)
            payload.setdefault('ide_type', 'ANTIGRAVITY')
            resources.append(AntigravityResource(**payload))
        return resources
