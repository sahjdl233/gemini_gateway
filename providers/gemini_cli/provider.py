"""GeminiCliProvider - Gateway adapter for Google Code Assist.

Protocol: cloudcode-pa.googleapis.com/v1internal (TASK-007).
Reference implementation in Su-kaka/gcli2api; here reimplemented from
scratch following the protocol conclusions in TASK-007.

This adapter ONLY orchestrates:
 1. asks GeminiCliClient to talk to the Code Assist endpoint
 2. the response envelope is unwrapped by response.py
 3. Provider errors are already classified (never raw HTTP)

TASK-ARCH-004: the Provider owns exactly ONE lifecycle-level
``HttpExecutionBackend``, which owns ONE persistent AsyncClient shared by
every GeminiCliResource::

    GeminiCliProvider
        -> HttpExecutionBackend
             -> ONE httpx.AsyncClient
                    resource A
                    resource B
                    resource C

Resources stay lightweight scheduling units: they contribute proxy config,
identity and project context, but never own transport.  A resource-scoped
OAuth adapter/token cache is intentional and does NOT make transport
resource-scoped.
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional

from core.health import HealthResult, HealthState
from core.models import ChatChunk, ChatRequest, ChatResponse, ModelInfo
from core.provider import Provider
from core.resource import Resource
from execution.base import ExecutionBackend
from execution.http import HttpExecutionBackend
from transport.proxy import ProxyConfig

from providers.gemini_cli.auth_adapter import GeminiCliAuthAdapter
from providers.gemini_cli.client import GeminiCliClient, DEFAULT_BASE_URL
from providers.gemini_cli.errors import GeminiCliProtocolError
from providers.gemini_cli.payload import build_envelope, get_model_name
from providers.gemini_cli.resource import GeminiCliResource
from providers.gemini_cli.response import parse_chunk, parse_response
from providers.gemini_cli.streaming import iter_chunks

logger = logging.getLogger(__name__)

DEFAULT_MODELS = ["gemini-2.5-flash", "gemini-2.5-pro"]

# Code Assist generation is long-running; keep the historical per-provider
# upstream budget.  The backend, not the Resource, owns it.
DEFAULT_TIMEOUT_SECONDS = 180.0


def _require_resource(resource: Resource) -> GeminiCliResource:
    if not isinstance(resource, GeminiCliResource):
        raise GeminiCliProtocolError(
            "gemini_cli: expected GeminiCliResource, got " + type(resource).__name__,
            provider="gemini_cli",
        )
    return resource


class _BackendHttpPost:
    """Minimal ``post(url, *, headers, data)`` surface over the shared backend.

    ``GeminiCliAuth`` refreshes through this bridge so the OAuth token
    endpoint call reuses the SAME pooled transport as API requests.  The
    backend itself stays unaware of OAuth; ``data`` is a form dict encoded
    here, exactly as ``transport.http`` clients used to receive it.
    """

    def __init__(self, backend: ExecutionBackend) -> None:
        self._backend = backend

    async def post(
        self,
        url: str,
        *,
        headers: Any = None,
        data: Any = None,
    ) -> Any:
        body = (
            urllib.parse.urlencode(data).encode("utf-8")
            if data is not None
            else None
        )
        return await self._backend.execute(
            "POST",
            url,
            headers=headers,
            data=body,
        )


class GeminiCliProvider(Provider):
    """Adapts the Code Assist protocol to the Gateway Provider interface.

    The provider owns exactly ONE HttpExecutionBackend for its whole
    lifetime, and that backend owns ONE persistent AsyncClient shared by
    every GeminiCliResource.
    """

    def __init__(
        self,
        *,
        models: Optional[List[str]] = None,
        credential_store: Optional[Any] = None,
        backend: Optional[ExecutionBackend] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        proxy: Optional[ProxyConfig] = None,
        base_headers: Optional[Mapping[str, str]] = None,
        max_connections: int = 20,
        max_keepalive_connections: int = 8,
    ) -> None:
        self._models: List[str] = list(models) if models else list(DEFAULT_MODELS)
        # One backend per provider, not per Resource.  ``_backend`` is
        # created lazily ONLY when no backend was injected, so a Provider
        # built purely for tests never opens a socket it will not use.
        self._backend: Optional[ExecutionBackend] = backend
        self._owns_backend: bool = backend is None
        self._backend_options: Dict[str, Any] = {
            "timeout_seconds": timeout_seconds,
            "proxy": proxy,
            "base_headers": base_headers,
            "max_connections": max_connections,
            "max_keepalive_connections": max_keepalive_connections,
        }
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

    @property
    def backend(self) -> ExecutionBackend:
        """The provider-owned ExecutionBackend (created on first use)."""
        return self._backend_for(None)

    def _backend_for(
        self,
        resource: Optional[GeminiCliResource] = None,
    ) -> ExecutionBackend:
        """Return the one shared backend, creating it on first use.

        Because there is exactly ONE backend per provider, a per-Resource
        proxy cannot be attached to the transport at request time.  The
        first Resource that materialises the backend therefore defines the
        egress, via ``proxy_config_for`` -> ``ProxyConfig``.  An explicit
        ``proxy=`` passed to the constructor always wins, and Resources
        without a proxy keep the historical direct connection.
        """
        if self._backend is None:
            options = dict(self._backend_options)
            if options.get("proxy") is None and resource is not None:
                options["proxy"] = self.proxy_config_for(resource)
            self._backend = HttpExecutionBackend(**options)
            self._owns_backend = True
        return self._backend

    async def close(self) -> None:
        """Provider lifecycle hook: close the provider-owned backend.

        Idempotent -- ``HttpExecutionBackend.close()`` is close-once, so
        repeated shutdowns are safe.  A backend injected for tests with
        ``owned=False`` never closes the external client.
        """
        backend = self._backend
        if backend is None:
            return
        await backend.close()

    def set_http_client(self, client: Any) -> None:
        """Inject an httpx.AsyncClient-compatible object (tests).

        Compatibility entry point: the client is handed to an
        ``HttpExecutionBackend`` with ``owned=False`` so the provider
        borrows the transport and never closes a test-owned client.  The
        per-resource bare-HTTP path is gone; every request still flows
        through the backend.
        """
        self._backend = HttpExecutionBackend(client=client, owned=False)
        self._owns_backend = False
        self._clients.clear()
        self._adapters.clear()

    async def _client_for(self, resource: GeminiCliResource) -> GeminiCliClient:
        cached = self._clients.get(resource.id)
        if cached is not None:
            return cached
        # Every Resource resolves the SAME provider-level backend.  Only
        # the auth adapter is resource-scoped.
        backend = self._backend_for(resource)
        adapter = GeminiCliAuthAdapter(
            http=_BackendHttpPost(backend),
            credential_store=self._credential_store,
        )
        self._adapters[resource.id] = adapter
        client = GeminiCliClient(backend=backend, auth=adapter.auth)
        self._clients[resource.id] = client
        return client

    async def _auth_adapter_for(self, resource: GeminiCliResource) -> GeminiCliAuthAdapter:
        """The per-resource auth adapter owning this resource's OAuth
        lifecycle (material resolution, refresh, invalidate)."""
        await self._client_for(resource)
        return self._adapters[resource.id]

    @staticmethod
    def proxy_config_for(resource: GeminiCliResource) -> Optional[ProxyConfig]:
        """Translate a Resource's proxy string into a ``ProxyConfig``.

        Proxy support is unchanged (TASK-ARCH-004 §4); only the owner
        changed.  The config is handed to ``HttpExecutionBackend``, which
        applies it to the ONE shared AsyncClient.  Unparsable values fall
        back to a direct connection exactly as before.
        """
        raw = getattr(resource, "proxy", None) or ""
        if not raw or not isinstance(raw, str) or not raw.strip():
            return None
        try:
            from urllib.parse import urlsplit

            sp = urlsplit(raw.strip())
            return ProxyConfig(
                scheme=sp.scheme or "socks5",
                host=sp.hostname,
                port=sp.port,
            )
        except Exception:  # noqa: BLE001
            return None

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
            try:
                async for chunk in iter_chunks(resp, model):
                    yield chunk
            finally:
                # The SSE parser (or a downstream consumer) may raise or
                # abandon the stream early; release the response context
                # either way.  The shared AsyncClient behind the backend is
                # untouched, so the backend stays reusable.
                await resp.aclose()
