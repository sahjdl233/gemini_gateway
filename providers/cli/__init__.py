"""CLI Provider adapter (reserved for TASK-007).

Interface only in TASK-000.
"""

from __future__ import annotations

from typing import AsyncIterator, List, Optional

from core.health import HealthResult
from core.models import ChatChunk, ChatRequest, ChatResponse, ModelInfo
from core.provider import Provider
from core.resource import Resource


class CLICredentialResource(Resource):
    """CLI routes: Credential / Account."""

    credential: Optional[str] = None


class CLIProvider(Provider):
    async def list_models(self) -> List[ModelInfo]:
        raise NotImplementedError("CLIProvider is reserved for TASK-007")

    async def complete(
        self, request: ChatRequest, resource: Resource
    ) -> ChatResponse:
        raise NotImplementedError("CLIProvider is reserved for TASK-007")

    async def stream(
        self, request: ChatRequest, resource: Resource
    ) -> AsyncIterator[ChatChunk]:
        raise NotImplementedError("CLIProvider is reserved for TASK-007")

    async def health_check(self, resource: Resource) -> HealthResult:
        raise NotImplementedError("CLIProvider is reserved for TASK-007")


__all__ = ["CLICredentialResource", "CLIProvider"]
