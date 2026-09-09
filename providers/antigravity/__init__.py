"""Antigravity Provider adapter (reserved for TASK-009).

Interface only in TASK-000.
"""

from __future__ import annotations

from typing import AsyncIterator, List, Optional

from core.health import HealthResult
from core.models import ChatChunk, ChatRequest, ChatResponse, ModelInfo
from core.provider import Provider
from core.resource import Resource


class AntigravityCredentialResource(Resource):
    """Antigravity routes: Credential / Account."""

    credential: Optional[str] = None


class AntigravityProvider(Provider):
    async def list_models(self) -> List[ModelInfo]:
        raise NotImplementedError("AntigravityProvider is reserved for TASK-009")

    async def complete(
        self, request: ChatRequest, resource: Resource
    ) -> ChatResponse:
        raise NotImplementedError("AntigravityProvider is reserved for TASK-009")

    async def stream(
        self, request: ChatRequest, resource: Resource
    ) -> AsyncIterator[ChatChunk]:
        raise NotImplementedError("AntigravityProvider is reserved for TASK-009")

    async def health_check(self, resource: Resource) -> HealthResult:
        raise NotImplementedError("AntigravityProvider is reserved for TASK-009")


__all__ = ["AntigravityCredentialResource", "AntigravityProvider"]
