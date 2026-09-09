"""AnonymousVertexProvider — the first real Google upstream adapter.

Implements the Anonymous Vertex / Agent Platform batchGraphql protocol.
The provider translates Gateway ChatRequest <-> internal models and maps
upstream errors to Gateway ProviderError types. It is transport-agnostic:
an httpx.AsyncClient-compatible object is injected for testability (tests
use a mock transport; production uses the gateway transport layer).
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from core.errors import (
    AuthenticationError,
    InvalidRequestError,
    ModelNotFoundError,
    ProviderError,
    RateLimitError,
    UpstreamUnavailableError,
)
from core.health import HealthResult, HealthState
from core.models import ChatChunk, ChatRequest, ChatResponse, ModelInfo, Usage
from core.provider import Provider
from core.resource import Resource

from providers.anonymous_vertex.protocol import (
    chat_to_vertex_request,
    get_model_spec,
    vertex_chunk_to_chat_chunk,
    vertex_response_to_chat_response,
)
from providers.anonymous_vertex.request import (
    build_batch_graphql_url,
    build_envelope,
)
from providers.anonymous_vertex.headers import build_xhr_headers
from providers.anonymous_vertex.resource import AnonymousVertexResource
from providers.anonymous_vertex.streaming import (
    StreamParseError,
    chunk_finish_reason,
    extract_chunk_from_frame,
    iter_chunks,
    normalize_chunk,
)
from providers.anonymous_vertex.errors import (
    UpstreamVertexError,
    classify_upstream_error,
    parse_upstream_error,
)

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

    # -- lifecycle / config --
    def set_http_client(self, client: Any) -> None:
        """Inject an httpx.AsyncClient-compatible object (tests)."""
        self._http = client

    def set_token_fetcher(self, fetcher) -> None:
        """Inject a recaptcha token fetcher (tests)."""
        self._token_fetcher = fetcher

    async def _ensure_client(self):
        """Return an HTTP client, building one if needed."""
        if self._http is not None:
            return self._http
        import httpx
        from transport.http import build_client
        from transport.proxy import TransportConfig
        self._http = build_client(TransportConfig(timeout_seconds=180.0))
        return self._http

    async def _get_token(self, resource: Resource) -> str:
        """Fetch a fresh recaptcha token for this request."""
        if self._token_fetcher is not None:
            return await self._token_fetcher(resource)
        from providers.anonymous_vertex.recaptcha import fetch_recaptcha_token
        client = await self._ensure_client()
        return await fetch_recaptcha_token(client=client)

    async def _make_request(
        self,
        client,
        model: str,
        gemini_request: dict,
        token: str,
        resource: Resource,
    ) -> Any:
        """POST the envelope to batchGraphql and return the response."""
        url = build_batch_graphql_url(self.api_key)
        envelope = build_envelope(model, gemini_request, token)
        headers = build_xhr_headers()
        body = json.dumps(envelope)
        resp = await client.post(
            url,
            content=body,
            headers=headers,
        )
        return resp

    def _map_http_error(
        self,
        status_code: int,
        body: bytes,
        retry_after: Optional[float] = None,
    ) -> ProviderError:
        """Map a non-200 HTTP response to a Gateway ProviderError.

        When the upstream returns HTTP 429 with a Retry-After header, the
        header value is carried on the RateLimitError so Core Runtime can
        honour it exactly (TASK-002 section 13).
        """
        parsed = parse_upstream_error(status_code, body)
        if retry_after is not None and parsed.retry_after is None:
            parsed.retry_after = retry_after
        return classify_upstream_error(parsed)

    @staticmethod
    def _extract_retry_after(resp: Any) -> Optional[float]:
        """Read the Retry-After response header (seconds), if present.

        httpx responses expose ``headers``; test mocks may omit it.  A
        missing/empty/invalid value yields None (Core Runtime falls back to
        exponential backoff).
        """
        headers = getattr(resp, "headers", None)
        if headers is None:
            return None
        try:
            raw = headers.get("Retry-After") or headers.get("retry-after")
        except (AttributeError, TypeError):
            return None
        if raw is None:
            return None
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

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
        gemini_request = chat_to_vertex_request(request)
        model = request.model
        resp = await self._make_request(client, model, gemini_request, token, resource)

        if resp.status_code != 200:
            raise self._map_http_error(
                resp.status_code,
                resp.content,
                self._extract_retry_after(resp),
            )

        # Parse NDJSON stream and collect chunks
        all_candidates: List[dict] = []
        usage_meta = None
        model_version = model
        response_id = ""
        async for chunk in iter_chunks(resp.aiter_bytes()):
            norm = normalize_chunk(chunk)
            if norm is None:
                continue
            frames = _flatten(norm)
            for item in frames:
                if not isinstance(item, dict):
                    continue
                # extract candidates from this frame
                if item.get("candidates"):
                    all_candidates.extend(item["candidates"])
                # capture metadata
                um = item.get("usageMetadata")
                if um:
                    usage_meta = um
                if item.get("modelVersion"):
                    model_version = item["modelVersion"]
                if item.get("responseId"):
                    response_id = item["responseId"]
            # check finish
            fr = chunk_finish_reason(norm)
            if fr:
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
        gemini_request = chat_to_vertex_request(request)
        model = gemini_request.get("model") or request.model
        resp = await self._make_request(client, model, gemini_request, token, resource)

        if resp.status_code != 200:
            raise self._map_http_error(
                resp.status_code,
                resp.content,
                self._extract_retry_after(resp),
            )

        usage_meta = None
        model_version = model
        response_id = ""
        async for chunk in iter_chunks(resp.aiter_bytes()):
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
                # emit final finish chunk
                final = vertex_chunk_to_chat_chunk(
                    [],
                    usage_metadata=usage_meta,
                    model_version=model_version,
                    response_id=response_id,
                )
                if final is not None:
                    final.finish_reason = fr
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


