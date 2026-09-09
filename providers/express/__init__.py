"""Express Provider adapter (reserved for TASK-006).

Interface only in TASK-000.
"""

from __future__ import annotations

from typing import AsyncIterator, List, Optional

from core.health import HealthResult
from core.models import ChatChunk, ChatRequest, ChatResponse, ModelInfo
from core.provider import Provider
from core.resource import Resource


class ExpressAccountResource(Resource):
    """Express routes: Account / Session."""

    session_id: Optional[str] = None


class ExpressProvider(Provider):
    async def list_models(self) -> List[ModelInfo]:
        raise NotImplementedError("ExpressProvider is reserved for TASK-006")

    async def complete(
        self, request: ChatRequest, resource: Resource
    ) -> ChatResponse:
        raise NotImplementedError("ExpressProvider is reserved for TASK-006")

    async def stream(
        self, request: ChatRequest, resource: Resource
    ) -> AsyncIterator[ChatChunk]:
        raise NotImplementedError("ExpressProvider is reserved for TASK-006")

    async def health_check(self, resource: Resource) -> HealthResult:
        raise NotImplementedError("ExpressProvider is reserved for TASK-006")


__all__ = ["ExpressAccountResource", "ExpressProvider"]
