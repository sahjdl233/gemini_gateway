"""FakeProvider: simulates success / 429 / auth_error / timeout / stream
without any real Google access (TASK-000 requirement 26)."""

from __future__ import annotations

from typing import AsyncIterator, List, Optional

from core.errors import (
    AuthenticationError,
    ModelNotFoundError,
    RateLimitError,
    TimeoutError,
)
from core.health import HealthResult, HealthState
from core.models import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    ModelInfo,
    Usage,
)
from core.provider import Provider
from core.resource import Resource


class FakeResource(Resource):
    """A Resource extended by the fake provider.

    scenario drives the behaviour per resource:
      success    -> always succeeds
      rate_limit -> always raises RateLimitError (retry_after from resource)
      auth_error -> always raises AuthenticationError (not retryable)
      timeout    -> always raises TimeoutError
      unknown_model -> raises ModelNotFoundError
    """

    scenario: str = "success"
    retry_after: Optional[float] = None
    reply_text: str = "Hello from FakeProvider!"


class FakeProvider(Provider):
    def __init__(self) -> None:
        pass

    async def list_models(self) -> List[ModelInfo]:
        return [
            ModelInfo(
                id="gemini-3.8-flash",
                provider="fake",
                capabilities={"stream": True, "vision": True, "tools": True},
            )
        ]

    def _scenario(self, resource: Resource) -> str:
        if isinstance(resource, FakeResource):
            return resource.scenario
        return "success"

    def _reply(self, resource: Resource) -> str:
        if isinstance(resource, FakeResource):
            return resource.reply_text
        return "Hello from FakeProvider!"

    async def complete(
        self, request: ChatRequest, resource: Resource
    ) -> ChatResponse:
        scenario = self._scenario(resource)
        if scenario == "rate_limit":
            retry_after = resource.retry_after if isinstance(resource, FakeResource) else None
            raise RateLimitError(
                "fake 429: rate limit",
                provider="fake",
                resource_id=resource.id,
                scope="resource",
                retry_after=retry_after,
            )
        if scenario == "auth_error":
            raise AuthenticationError(
                "fake 401: invalid credentials",
                provider="fake",
                resource_id=resource.id,
            )
        if scenario == "timeout":
            raise TimeoutError(
                "fake timeout", provider="fake", resource_id=resource.id
            )
        if scenario == "unknown_model":
            raise ModelNotFoundError(
                f"model '{request.model}' not found (fake)",
                provider="fake",
                resource_id=resource.id,
            )
        text = self._reply(resource)
        return ChatResponse(
            id=f"chatcmpl-fake-{resource.id}",
            model=request.model,
            text=text,
            finish_reason="stop",
            usage=Usage(
                prompt_tokens=len(request.messages),
                completion_tokens=len(text.split()),
                total_tokens=len(request.messages) + len(text.split()),
            ),
        )

    async def stream(
        self, request: ChatRequest, resource: Resource
    ) -> AsyncIterator[ChatChunk]:
        scenario = self._scenario(resource)
        if scenario == "rate_limit":
            retry_after = resource.retry_after if isinstance(resource, FakeResource) else None
            raise RateLimitError(
                "fake 429: rate limit",
                provider="fake",
                resource_id=resource.id,
                scope="resource",
                retry_after=retry_after,
            )
        if scenario == "auth_error":
            raise AuthenticationError(
                "fake 401: invalid credentials",
                provider="fake",
                resource_id=resource.id,
            )
        if scenario == "timeout":
            raise TimeoutError(
                "fake timeout", provider="fake", resource_id=resource.id
            )
        if scenario == "unknown_model":
            raise ModelNotFoundError(
                f"model '{request.model}' not found (fake)",
                provider="fake",
                resource_id=resource.id,
            )
        text = self._reply(resource)
        words = text.split()
        for i, word in enumerate(words):
            yield ChatChunk(
                id=f"chatcmpl-fake-{resource.id}",
                model=request.model,
                text=word + " ",
            )
        yield ChatChunk(
            id=f"chatcmpl-fake-{resource.id}",
            model=request.model,
            text=None,
            finish_reason="stop",
            usage=Usage(
                prompt_tokens=len(request.messages),
                completion_tokens=len(words),
                total_tokens=len(request.messages) + len(words),
            ),
        )

    async def health_check(self, resource: Resource) -> HealthResult:
        state = HealthState.HEALTHY
        if isinstance(resource, FakeResource) and resource.scenario in (
            "rate_limit",
            "timeout",
            "auth_error",
        ):
            state = HealthState.DEGRADED
        return HealthResult(state=state, message=f"fake health for {resource.id}")
