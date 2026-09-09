"""Build Provider adapter (reserved for TASK-008).

Interface only in TASK-000.
"""

from __future__ import annotations

from typing import AsyncIterator, List, Optional

from core.health import HealthResult
from core.models import ChatChunk, ChatRequest, ChatResponse, ModelInfo
from core.provider import Provider
from core.resource import Resource


class BuildSessionResource(Resource):
    """Build routes: Account / Browser Session."""

    browser_session: Optional[str] = None


class BuildProvider(Provider):
    async def list_models(self) -> List[ModelInfo]:
        raise NotImplementedError("BuildProvider is reserved for TASK-008")

    async def complete(
        self, request: ChatRequest, resource: Resource
    ) -> ChatResponse:
        raise NotImplementedError("BuildProvider is reserved for TASK-008")

    async def stream(
        self, request: ChatRequest, resource: Resource
    ) -> AsyncIterator[ChatChunk]:
        raise NotImplementedError("BuildProvider is reserved for TASK-008")

    async def health_check(self, resource: Resource) -> HealthResult:
        raise NotImplementedError("BuildProvider is reserved for TASK-008")


__all__ = ["BuildSessionResource", "BuildProvider"]
