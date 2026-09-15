from __future__ import annotations

from typing import Any

from core.model_registry import ModelInfo
from core.provider import Provider

from .client import AntigravityClient
from .model_discovery import ModelDiscovery
from .resource import AntigravityResource


class AntigravityProvider(Provider):
    def __init__(self, resource: AntigravityResource, client: AntigravityClient | None = None) -> None:
        self.resource = resource
        self.client = client or AntigravityClient(resource)
        self.discovery = ModelDiscovery(self.client)

    def list_models(self) -> list[ModelInfo]:
        return self.discovery.fetch_models()

    def generateContent(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Antigravity generateContent is not implemented yet")

    def streamGenerateContent(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Antigravity streamGenerateContent is not implemented yet")
