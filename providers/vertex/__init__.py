"""Vertex Provider adapter (reserved for TASK-004).

TASK-000 only pre-defines the interface.  Do NOT reverse-engineer the
Vertex session / Recaptcha flow here.
"""

from __future__ import annotations

from typing import AsyncIterator, List, Optional

from core.health import HealthResult
from core.models import ChatChunk, ChatRequest, ChatResponse, ModelInfo
from core.provider import Provider
from core.resource import Resource


class VertexSessionResource(Resource):
    """Vertex routes: Egress / Session."""

    session_id: Optional[str] = None  # type: ignore # placeholder
    egress: Optional[str] = None


class VertexProvider(Provider):
    async def list_models(self) -> List[ModelInfo]:
        raise NotImplementedError("VertexProvider is reserved for TASK-004")

    async def complete(
        self, request: ChatRequest, resource: Resource
    ) -> ChatResponse:
        raise NotImplementedError("VertexProvider is reserved for TASK-004")

    async def stream(
        self, request: ChatRequest, resource: Resource
    ) -> AsyncIterator[ChatChunk]:
        raise NotImplementedError("VertexProvider is reserved for TASK-004")

    async def health_check(self, resource: Resource) -> HealthResult:
        raise NotImplementedError("VertexProvider is reserved for TASK-004")


__all__ = ["VertexSessionResource", "VertexProvider"]
