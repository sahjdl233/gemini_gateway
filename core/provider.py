"""Provider Adapter contract.

All providers (fake, firebase, vertex, express, cli, build,
antigravity) MUST implement this abstraction.  The Scheduler only talks
to 'Provider' and never to a concrete Google implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, List

from .health import HealthResult
from .models import ChatChunk, ChatRequest, ChatResponse, ModelInfo
from .resource import Resource


class Provider(ABC):
    @abstractmethod
    async def list_models(self) -> List[ModelInfo]:
        """Models this provider can serve."""

    @abstractmethod
    async def complete(
        self, request: ChatRequest, resource: Resource
    ) -> ChatResponse:
        """Non-streaming completion."""

    @abstractmethod
    async def stream(
        self, request: ChatRequest, resource: Resource
    ) -> AsyncIterator[ChatChunk]:
        """Streaming completion as internal ChatChunk objects."""

    @abstractmethod
    async def health_check(self, resource: Resource) -> HealthResult:
        """Probe the health of a single resource."""
