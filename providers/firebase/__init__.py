"""Firebase Provider adapter (reserved for TASK-002).

TASK-000 defines the interface and the resource model only.  No real
Google / Firebase access is implemented at this stage.
"""

from __future__ import annotations

from typing import AsyncIterator, List, Optional

from core.health import HealthResult
from core.models import ChatChunk, ChatRequest, ChatResponse, ModelInfo
from core.provider import Provider
from core.resource import Resource


class FirebaseProjectResource(Resource):
    """Firebase AI Logic is quota'd at the project level, not per app/IP."""

    project_id: str = ""
    app_id: str = ""
    api_key: str = ""
    app_check: Optional[str] = None
    proxy: Optional[str] = None


class FirebaseProvider(Provider):
    async def list_models(self) -> List[ModelInfo]:
        raise NotImplementedError("FirebaseProvider is reserved for TASK-002")

    async def complete(
        self, request: ChatRequest, resource: Resource
    ) -> ChatResponse:
        raise NotImplementedError("FirebaseProvider is reserved for TASK-002")

    async def stream(
        self, request: ChatRequest, resource: Resource
    ) -> AsyncIterator[ChatChunk]:
        raise NotImplementedError("FirebaseProvider is reserved for TASK-002")

    async def health_check(self, resource: Resource) -> HealthResult:
        raise NotImplementedError("FirebaseProvider is reserved for TASK-002")


__all__ = ["FirebaseProjectResource", "FirebaseProvider"]
