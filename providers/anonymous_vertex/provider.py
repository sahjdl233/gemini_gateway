"""AnonymousVertexProvider — the first real Google upstream adapter.

Implements the Anonymous Vertex / Agent Platform batchGraphql protocol
through a layered protocol stack:

    AnonymousVertexProvider
            |
            v
    AnonymousVertexClient   (client.py)
            |
            v
    AnonymousVertexProtocol (protocol.py)
            |
            v
    HTTP Transport          (transport.py)

The Provider ONLY orchestrates: it converts the Gateway ChatRequest into
the internal AnonymousVertexRequest (request.py), asks the AnonymousVertexClient
to talk to upstream, and converts the returned Gemini frames into Gateway
chunks (response.py).  It never builds GraphQL payloads or Google headers
itself (TASK-002-A section 18).
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, List, Optional

from core.errors import UpstreamUnavailableError
from core.health import HealthResult, HealthState
from core.models import ChatChunk, ChatRequest, ChatResponse, ModelInfo
from core.provider import Provider
from core.resource import Resource

from providers.anonymous_vertex.client import AnonymousVertexClient
from providers.anonymous_vertex.request import chat_to_vertex_request
from providers.anonymous_vertex.response import (
    map_finish_reason,
    vertex_chunk_to_chat_chunk,
    vertex_response_to_chat_response,
)
from providers.anonymous_vertex.resource import AnonymousVertexResource
from providers.anonymous_vertex.streaming import (
    chunk_finish_reason,
    normalize_chunk,
)
from providers.anonymous_vertex.transport import HttpxTransport

logger = logging.getLogger(__name__)

# Default anonymous API key (public Google constant; not a user secret).
ANON_API_KEY = "AIzaSyCI-zsRP85UVOi0DjtiCwWBwQ1djDy741g"

# Text-family models supported (from source config/models.json).
TEXT_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
]


class AnonymousVertexProvider(Provider):
    """Adapter for the Anonymous Vertex batchGraphql endpoint."""

    def __init__(
        self,
        *,
        api_key: str = "",
        http_client: Optional[Any] = None,
        token_fetcher=None,
        models: Optional[List[str]] = None,
    ) -> None:
        self.api_key = api_key or ANON_API_KEY
        self._http = http_client
        self._token_fetcher = token_fetcher
        self._models = list(models) if models else list(TEXT_MODELS)
        self._client: Optional[AnonymousVertexClient] = None

    # -- lifecycle / resource wiring --

    def set_http_client(self, client: Any) -> None:
        """Inject an httpx.AsyncClient-compatible object (tests)."""
        self._http = client
        self._client = None

    def set_token_fetcher(self, fetcher) -> None:
        """Inject a recaptcha token fetcher (tests)."""
        self._token_fetcher = fetcher

    async def _ensure_http(self):
        """Return the raw HTTP client, building one if needed."""
        if self._http is not None:
            return self._http
        from transport.http import build_client
        from transport.proxy import TransportConfig

        self._http = build_client(TransportConfig(timeout_seconds=180.0))
        return self._http

    async def _ensure_client(self) -> AnonymousVertexClient:
        """Return (and cache) the protocol client for this provider."""
        if self._client is None:
            http = await self._ensure_http()
            self._client = AnonymousVertexClient(
                transport=HttpxTransport(http),
                api_key=self.api_key,
            )
        return self._client

    async def _get_token(self, resource: Resource) -> str:
        """Fetch a fresh recaptcha token for this request."""
        if self._token_fetcher is not None:
            return await self._token_fetcher(resource)
        from providers.anonymous_vertex.recaptcha import fetch_recaptcha_token

        http = await self._ensure_http()
        try:
            return await fetch_recaptcha_token(client=http)
        except Exception as exc:
            # A failed anchor/reload flow is a transient upstream-side
            # failure; surface it as a retryable ProviderError so the
            # Scheduler cools the resource down and falls back instead of
            # leaking a raw RuntimeError (which is not a ProviderError and
            # would skip failure bookkeeping entirely).
            raise UpstreamUnavailableError(
                f"recaptcha token fetch failed: {exc}", provider="anonymous_vertex"
            ) from exc

    # -- Provider interface --

    async def list_models(self) -> List[ModelInfo]:
        """Models served by this provider (config-based; no upstream list API)."""
        return [
            ModelInfo(id=m, provider="anonymous_vertex", capabilities={"stream": True})
            for m in self._models
        ]

    async def health_check(self, resource: Resource) -> HealthResult:
        """Health is based on resource state; no live upstream probe."""
        if resource.health in (HealthState.COOLDOWN, HealthState.DISABLED):
            return HealthResult(state=resource.health, message="resource unavailable")
        return HealthResult(state=HealthState.HEALTHY, message="anonymous_vertex ok")

    async def complete(
        self, request: ChatRequest, resource: Resource
    ) -> ChatResponse:
        """Non-streaming completion (collects upstream stream chunks)."""
        client = await self._ensure_client()
        token = await self._get_token(resource)
        vertex_request = chat_to_vertex_request(request)

        all_candidates: List[dict] = []
        usage_meta: Optional[dict] = None
        model_version = request.model
        response_id = ""

        async for chunk in client.stream_content(vertex_request, token):
            norm = normalize_chunk(chunk)
            if norm is None:
                continue
            for item in _flatten(norm):
                if not isinstance(item, dict):
                    continue
                if item.get("candidates"):
                    all_candidates.extend(item["candidates"])
                um = item.get("usageMetadata")
                if um:
                    usage_meta = um
                if item.get("modelVersion"):
                    model_version = item["modelVersion"]
                if item.get("responseId"):
                    response_id = item["responseId"]
            if chunk_finish_reason(norm):
                break

        if not all_candidates:
            raise UpstreamUnavailableError(
                "anonymous vertex returned no content", provider="anonymous_vertex"
            )

        return vertex_response_to_chat_response(
            all_candidates,
            usage_metadata=usage_meta,
            model_version=model_version,
            response_id=response_id,
        )

    async def stream(
        self, request: ChatRequest, resource: Resource
    ) -> AsyncIterator[ChatChunk]:
        """Streaming completion from upstream NDJSON frames."""
        client = await self._ensure_client()
        token = await self._get_token(resource)
        vertex_request = chat_to_vertex_request(request)

        usage_meta: Optional[dict] = None
        model_version = request.model
        response_id = ""

        async for chunk in client.stream_content(vertex_request, token):
            norm = normalize_chunk(chunk)
            if norm is None:
                continue
            fr = chunk_finish_reason(norm)
            for item in _flatten(norm):
                if not isinstance(item, dict):
                    continue
                if item.get("usageMetadata"):
                    usage_meta = item["usageMetadata"]
                if item.get("modelVersion"):
                    model_version = item["modelVersion"]
                if item.get("responseId"):
                    response_id = item["responseId"]
                chat_chunk = vertex_chunk_to_chat_chunk(
                    item.get("candidates") or [],
                    usage_metadata=item.get("usageMetadata"),
                    model_version=model_version,
                    response_id=response_id,
                )
                if chat_chunk is not None:
                    yield chat_chunk
            if fr:
                final = vertex_chunk_to_chat_chunk(
                    [],
                    usage_metadata=usage_meta,
                    model_version=model_version,
                    response_id=response_id,
                )
                if final is not None:
                    final.finish_reason = map_finish_reason(fr)
                    yield final
                return


def trim(model: str) -> str:
    from providers.anonymous_vertex.signature import trim_gemini_path_prefix

    return trim_gemini_path_prefix(model)


def _flatten(norm: Any) -> List[Any]:
    """Flatten a normalized chunk (which may be a list of items) to a list."""
    if isinstance(norm, list):
        return norm
    return [norm]

