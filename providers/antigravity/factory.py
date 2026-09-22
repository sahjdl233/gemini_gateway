from __future__ import annotations

# __TASK-ANTIGRAVITY-001__: factory wiring lifecycle (debug-marker)

from typing import Any, List

from .provider import AntigravityProvider
from .resource import AntigravityResource


class AntigravityProviderFactory:
    def create_provider(self, provider_id: str, config: Any = None) -> AntigravityProvider:
        # Provider must not bind to any account/resource.
        # Scheduler will inject selected AntigravityResource into complete()/stream().
        return AntigravityProvider()


class AntigravityResourceFactory:
    def create_resources(self, provider_id: str, config: list) -> list:
        resources = []
        for item in config:
            payload = dict(item)
            payload.setdefault('provider', provider_id)
            payload.setdefault('ide_type', 'ANTIGRAVITY')
            resources.append(AntigravityResource(**payload))
        return resources
