from __future__ import annotations

from typing import Any

from .provider import AntigravityProvider
from .resource import AntigravityResource


class AntigravityFactory:
    name = "antigravity"

    def __init__(self) -> None:
        self.resource = AntigravityResource(id="antigravity")

    def create_resource(self) -> AntigravityResource:
        return AntigravityResource(id="antigravity")

    def create_provider(self, resource: Any = None) -> AntigravityProvider:
        res = resource or self.create_resource()
        return AntigravityProvider(res)


ProviderFactory = AntigravityFactory
ResourceFactory = AntigravityFactory
