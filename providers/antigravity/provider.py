from __future__ import annotations

from typing import Any, AsyncIterator, Mapping, Optional

from core.health import HealthResult, HealthState
from core.model_registry import ModelInfo
from core.models import ChatChunk, ChatRequest, ChatResponse
from core.provider import Provider
from execution.http import HttpExecutionBackend
from transport.proxy import ProxyConfig

from .client import AntigravityClient
from .model_discovery import ModelDiscovery
from .resource import AntigravityResource


class AntigravityProvider(Provider):
    """Antigravity adapter.

    The provider owns exactly ONE HttpExecutionBackend, which owns ONE
    persistent AsyncClient shared by every AntigravityResource. The
    Scheduler still only sees Provider + Resource.
    """

    def __init__(
        self,
        client: AntigravityClient | None = None,
        backend: Optional[HttpExecutionBackend] = None,
        *,
        timeout_seconds: float = 30.0,
        proxy: Optional[ProxyConfig] = None,
        base_headers: Optional[Mapping[str, str]] = None,
        max_connections: int = 20,
        max_keepalive_connections: int = 8,
    ) -> None:
        self.backend = backend or HttpExecutionBackend(
            timeout_seconds=timeout_seconds,
            proxy=proxy,
            base_headers=base_headers,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )
        self.client = client or AntigravityClient(backend=self.backend)
        self.discovery = ModelDiscovery(self.client)

    async def list_models(self) -> list[ModelInfo]:
        return await self.discovery.fetch_models()

    async def close(self) -> None:
        """Release the provider-owned backend (one AsyncClient)."""
        await self.backend.close()

    async def complete(self, request: ChatRequest, resource: Any) -> ChatResponse:
        raise NotImplementedError("Antigravity complete is not implemented yet")

    async def stream(self, request: ChatRequest, resource: Any) -> AsyncIterator[ChatChunk]:
        raise NotImplementedError("Antigravity stream is not implemented yet")
        yield

    async def health_check(self, resource: Any) -> HealthResult:
        return HealthResult(state=HealthState.HEALTHY)
