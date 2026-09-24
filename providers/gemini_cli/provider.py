"""GeminiCliProvider - Gateway adapter for Google Code Assist.

Protocol: cloudcode-pa.googleapis.com/v1internal (TASK-007).
Reference implementation in Su-kaka/gcli2api; here reimplemented from
scratch following the protocol conclusions in TASK-007.

This adapter ONLY orchestrates:
  1. asks GeminiCliClient to talk to the Code Assist endpoint
  2. the response envelope is unwrapped by response.py
  3. Provider errors are already classified (never raw HTTP)
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from core.health import HealthResult, HealthState
from core.models import ChatChunk, ChatRequest, ChatResponse, ModelInfo
from core.provider import Provider
from core.resource import Resource

from providers.gemini_cli.auth_adapter import GeminiCliAuthAdapter
from providers.gemini_cli.client import GeminiCliClient, DEFAULT_BASE_URL
from providers.gemini_cli.errors import GeminiCliProtocolError
from providers.gemini_cli.payload import build_envelope, get_model_name
from providers.gemini_cli.resource import GeminiCliResource
from providers.gemini_cli.response import parse_chunk, parse_response
from providers.gemini_cli.streaming import iter_chunks

logger = logging.getLogger(__name__)

DEFAULT_MODELS = ["gemini-2.5-flash", "gemini-2.5-pro"]


def _require_resource(resource: Resource) -> GeminiCliResource:
    if not isinstance(resource, GeminiCliResource):
        raise GeminiCliProtocolError(
            "gemini_cli: expected GeminiCliResource, got " + type(resource).__name__,
            provider="gemini_cli",
        )
    return resource


class GeminiCliProvider(Provider):
    """Adapts the Code Assist protocol to the Gateway Provider interface."""

    def __init__(
        self,
        *,
        models: Optional[List[str]] = None,
        credential_store: Optional[Any] = None,
    ) -> None:
        self._models: List[str] = list(models) if models else list(DEFAULT_MODELS)
        self._http: Optional[Any] = None
        self._clients: Dict[str, GeminiCliClient] = {}
        # Per-resource GeminiCliAuthAdapter instances (AUTH-004).  The
        # adapter owns the OAuth lifecycle and the credential->material
        # resolution; the Provider itself owns no auth logic.  Cache scope
        # is resource-scoped: one adapter/token cache per Resource, even
        # when Resources share a credential_id.
        self._adapters: Dict[str, GeminiCliAuthAdapter] = {}
        self._credential_store = credential_store

    def set_credential_store(self, store: Any) -> None:
        """Attach the application-wide credential store (AUTH-002)."""
        self._credential_store = store
        self._clients.clear()
        self._adapters.clear()

    def set_http_client(self, client: Any) -> None:
        """Inject an httpx.AsyncClient-compatible object (tests)."""
        self._http = client
        self._clients.clear()
        self._adapters.clear()

    async def _client_for(self, resource: GeminiCliResource) -> GeminiCliClient:
        cached = self._clients.get(resource.id)
        if cached is not None:
            return cached
        http = self._http
        if http is None:
            http = self._build_http(resource)
        adapter = GeminiCliAuthAdapter(
            http=http,
            credential_store=self._credential_store,
        )
        self._adapters[resource.id] = adapter
        client = GeminiCliClient(http=http, auth=adapter.auth)
        self._clients[resource.id] = client
        return client

    async def _auth_adapter_for(self, resource: GeminiCliResource) -> GeminiCliAuthAdapter:
        """The per-resource auth adapter owning this resource's OAuth
        lifecycle (material resolution, refresh, invalidate)."""
        await self._client_for(resource)
        return self._adapters[resource.id]

    def _build_http(self, resource: GeminiCliResource) -> Any:
        """Build httpx.AsyncClient, optionally using the resource proxy."""
        from transport.http import build_client
        from transport.proxy import ProxyConfig, TransportConfig

        proxy_cfg = None
        raw = getattr(resource, "proxy", None) or ""
        if raw and isinstance(raw, str) and raw.strip():
            try:
                from urllib.parse import urlsplit
                sp = urlsplit(raw)
                proxy_cfg = ProxyConfig(
                    scheme=sp.scheme or "socks5",
                    host=sp.hostname,
                    port=sp.port,
                )
            except Exception:  # noqa: BLE001
                proxy_cfg = None
        return build_client(TransportConfig(proxy=proxy_cfg, timeout_seconds=180.0))

    async def _ensure_project(self, resource: GeminiCliResource) -> None:
        """Ensure resource has a project_id, running loadCodeAssist/onboard if needed."""
        if resource.project_id:
            return
        client = await self._client_for(resource)
        from providers.gemini_cli.onboard import discover_project
        resource.project_id = await discover_project(client, resource)

    # -- Provider interface -------------------------------------------------

    async def list_models(self) -> List[ModelInfo]:
        return [
            ModelInfo(
                id=m,
                provider="gemini_cli",
                capabilities={"stream": True, "tools": True},
            )
            for m in self._models
        ]

    async def health_check(self, resource: Resource) -> HealthResult:
        res = _require_resource(resource)
        if res.health in (HealthState.COOLDOWN, HealthState.DISABLED):
            return HealthResult(
                state=res.health, message="resource unavailable"
            )
        return HealthResult(state=HealthState.HEALTHY, message="gemini_cli ok")

    async def complete(
        self,
        request: ChatRequest,
        resource: Resource,
    ) -> ChatResponse:
        res = _require_resource(resource)
        await self._ensure_project(res)
        if not res.project_id:
            raise GeminiCliProtocolError(
                "gemini_cli: onboarding failed to produce project_id",
                provider="gemini_cli",
                resource_id=res.id,
            )
        client = await self._client_for(res)
        model = get_model_name(request)
        base_url = DEFAULT_BASE_URL
        envelope = build_envelope(request, project=res.project_id, model=model)
        resp = await client.post(res, base_url, envelope)
        return parse_response(resp.json(), model)

    async def stream(
        self,
        request: ChatRequest,
        resource: Resource,
    ) -> AsyncIterator[ChatChunk]:
        res = _require_resource(resource)
        await self._ensure_project(res)
        if not res.project_id:
            raise GeminiCliProtocolError(
                "gemini_cli: onboarding failed to produce project_id",
                provider="gemini_cli",
                resource_id=res.id,
            )
        client = await self._client_for(res)
        model = get_model_name(request)
        base_url = DEFAULT_BASE_URL
        envelope = build_envelope(request, project=res.project_id, model=model)
        async for resp in client.stream(res, base_url, envelope):
            async for chunk in iter_chunks(resp, model):
                yield chunk
