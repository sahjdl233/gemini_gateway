from __future__ import annotations

from typing import Any, AsyncIterator

from core.health import HealthResult, HealthState
from core.model_registry import ModelInfo
from core.models import ChatChunk, ChatRequest, ChatResponse
from core.provider import Provider

from .client import AntigravityClient
from .model_discovery import ModelDiscovery
from .resource import AntigravityResource


class AntigravityProvider(Provider):
    def __init__(self, client: AntigravityClient | None = None) -> None:
        self.client = client or AntigravityClient()
        self.discovery = ModelDiscovery(self.client)

    async def list_models(self) -> list[ModelInfo]:
        return self.discovery.fetch_models()

    async def complete(self, request: ChatRequest, resource: Any) -> ChatResponse:
        raise NotImplementedError("Antigravity complete is not implemented yet")

    async def stream(self, request: ChatRequest, resource: Any) -> AsyncIterator[ChatChunk]:
        raise NotImplementedError("Antigravity stream is not implemented yet")
        yield

    async def health_check(self, resource: Any) -> HealthResult:
        return HealthResult(state=HealthState.HEALTHY)