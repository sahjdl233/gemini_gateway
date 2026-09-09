"""FakeProvider: simulates success / 429 / 401 / 403 / 404 / 500 / timeout
and stream scenarios without any real Google access."""

from __future__ import annotations

import logging
from typing import AsyncIterator, List, Optional

from core.errors import (
    AuthenticationError,
    AuthorizationError,
    ContentFilterError,
    ModelNotFoundError,
    RateLimitError,
    TimeoutError,
    UpstreamUnavailableError,
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

logger = logging.getLogger(__name__)


class FakeResource(Resource):
    """A Resource extended by the fake provider.

    scenario drives the behaviour per resource:
      success    -> always succeeds
      rate_limit -> always raises RateLimitError (retry_after from resource)
      auth_error -> always raises AuthenticationError (not retryable)
      authz_error -> always raises AuthorizationError (not retryable)
      forbidden  -> raises AuthorizationError (alias for authz_error)
      timeout    -> always raises TimeoutError
      server_error -> raises UpstreamUnavailableError (retryable)
      content_filter -> raises ContentFilterError (not retryable)
      unknown_model -> raises ModelNotFoundError
    """

    scenario: str = "success"
    retry_after: Optional[float] = None
    reply_text: str = "Hello from FakeProvider!"
    model_ids: Optional[List[str]] = None


class FakeProvider(Provider):
    def __init__(self) -> None:
        pass

    async def list_models(self) -> List[ModelInfo]:
        return [
            ModelInfo(
                id="gemini-3.8-flash",
                provider="fake",
                capabilities={"stream": True, "vision": True, "tools": True},
            ),
            ModelInfo(
                id="gemini-test",
                provider="fake",
                capabilities={"stream": True, "vision": False, "tools": False},
            ),
            ModelInfo(
                id="gemini-other",
                provider="fake",
                capabilities={"stream": True, "vision": False, "tools": True},
            ),
        ]

    def _scenario(self, resource: Resource) -> str:
        if isinstance(resource, FakeResource):
            return resource.scenario
        return "success"

    def _reply(self, resource: Resource) -> str:
        if isinstance(resource, FakeResource):
            return resource.reply_text
        return "Hello from FakeProvider!"

    def _raise_if_error(self, scenario: str, resource: Resource, request: Optional[ChatRequest] = None) -> None:
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
        if scenario in ("authz_error", "forbidden"):
            raise AuthorizationError(
                "fake 403: not allowed",
                provider="fake",
                resource_id=resource.id,
            )
        if scenario == "timeout":
            raise TimeoutError(
                "fake timeout", provider="fake", resource_id=resource.id
            )
        if scenario == "server_error":
            raise UpstreamUnavailableError(
                "fake 500: upstream error",
                provider="fake",
                resource_id=resource.id,
            )
        if scenario == "content_filter":
            raise ContentFilterError(
                "fake content filtered",
                provider="fake",
                resource_id=resource.id,
            )
        if scenario == "unknown_model":
            model = request.model if request else "unknown"
            raise ModelNotFoundError(
                f"model '{model}' not found (fake)",
                provider="fake",
                resource_id=resource.id,
            )

    async def complete(
        self, request: ChatRequest, resource: Resource
    ) -> ChatResponse:
        scenario = self._scenario(resource)
        self._raise_if_error(scenario, resource, request)
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
        self._raise_if_error(scenario, resource, request)
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
            "authz_error",
            "forbidden",
            "server_error",
            "content_filter",
        ):
            state = HealthState.DEGRADED
        return HealthResult(state=state, message=f"fake health for {resource.id}")
