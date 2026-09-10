"""FirebaseProvider — Gateway-native adapter for Firebase AI Logic.

Implements core.Provider so one Firebase Project maps to one
FirebaseResource (TASK-003). The provider ONLY orchestrates: it converts
the Gateway ChatRequest into a Gemini generateContent payload, asks the
FirebaseClient to talk to upstream, and converts Gemini JSON/SSE frames
into Gateway ChatResponse/ChatChunk objects.

    FirebaseProvider
        ├── FirebaseResource       (resource.py)  one Firebase Project
        ├── FirebaseAuth           (auth.py)      App Check debug token → JWT
        ├── FirebaseClient         (client.py)    firebasevertexai.googleapis.com
        ├── FirebasePayloadBuilder (payload.py)   OpenAI → Gemini
        ├── FirebaseResponseParser (response.py)  Gemini → ChatResponse/Chunk
        └── FirebaseErrorMapper    (errors.py)    Google error → ProviderError
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, List, Optional
from urllib.parse import urlsplit

from core.health import HealthResult, HealthState
from core.models import ChatChunk, ChatRequest, ChatResponse, ModelInfo
from core.provider import Provider
from core.resource import Resource

from providers.firebase.auth import FirebaseAuth
from providers.firebase.client import FirebaseClient
from providers.firebase.payload import build_payload, get_model_name
from providers.firebase.resource import FirebaseResource
from providers.firebase.response import parse_chunk, parse_response

logger = logging.getLogger(__name__)

# Config-driven default model snapshot (TASK-003: do NOT copy firebase2api's
# advertised model table; these ids are project-dependent and the provider
# relies on the configured model list / upstream 404 to validate).
DEFAULT_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash"]


class FirebaseProvider(Provider):
    """Adapter for the Firebase AI Logic (firebasevertexai) upstream."""

    def __init__(
        self,
        *,
        models: Optional[List[str]] = None,
        http_client: Optional[Any] = None,
    ) -> None:
        self._models = list(models) if models else list(DEFAULT_MODELS)
        self._http = http_client
        self._clients: Dict[str, FirebaseClient] = {}

    # -- lifecycle / resource wiring --

    def set_http_client(self, client: Any) -> None:
        """Inject an httpx.AsyncClient-compatible object (tests)."""
        self._http = client
        self._clients.clear()

    async def _client_for(self, resource: FirebaseResource) -> FirebaseClient:
        """Return (and cache) the client + auth bound to one resource."""
        cached = self._clients.get(resource.id)
        if cached is not None:
            return cached
        http = self._http
        if http is None:
            http = self._build_http(resource)
        auth = FirebaseAuth(client=http)
        client = FirebaseClient(http=http, auth=auth)
        self._clients[resource.id] = client
        return client

    def _build_http(self, resource: FirebaseResource) -> Any:
        """Build an httpx client honouring the resource's optional proxy."""
        from transport.http import build_client
        from transport.proxy import ProxyConfig, TransportConfig

        proxy = _proxy_config_from_url(resource.proxy)
        return build_client(
            TransportConfig(proxy=proxy, timeout_seconds=180.0)
        )

    # -- Provider interface --

    async def list_models(self) -> List[ModelInfo]:
        """Models served by this provider (config snapshot; no list API)."""
        return [
            ModelInfo(
                id=m,
                provider="firebase",
                capabilities={"stream": True, "tools": True},
            )
            for m in self._models
        ]

    async def health_check(self, resource: Resource) -> HealthResult:
        """Health is based on resource state; no live upstream probe."""
        if resource.health in (HealthState.COOLDOWN, HealthState.DISABLED):
            return HealthResult(state=resource.health, message="resource unavailable")
        return HealthResult(state=HealthState.HEALTHY, message="firebase ok")

    async def complete(
        self, request: ChatRequest, resource: Resource
    ) -> ChatResponse:
        """Non-streaming completion: OpenAI → Gemini → ChatResponse."""
        fb = _require_resource(resource)
        client = await self._client_for(fb)
        model = get_model_name(request)
        payload = build_payload(request)
        resp = await client.complete(fb, model, payload)
        return parse_response(resp.json(), model)

    async def stream(
        self, request: ChatRequest, resource: Resource
    ) -> AsyncIterator[ChatChunk]:
        """Streaming completion: Firebase SSE → ChatChunk."""
        fb = _require_resource(resource)
        client = await self._client_for(fb)
        model = get_model_name(request)
        payload = build_payload(request)
        async for event in client.stream(fb, model, payload):
            chunk = parse_chunk(event, model)
            if chunk is not None:
                yield chunk


def _require_resource(resource: Resource) -> FirebaseResource:
    if not isinstance(resource, FirebaseResource):
        raise TypeError(
            "firebase provider requires a FirebaseResource, got "
            + type(resource).__name__
        )
    return resource


def _proxy_config_from_url(url: Optional[str]):
    """Parse a proxy URL string into transport.ProxyConfig (or None)."""
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.hostname:
        return None
    scheme = (parts.scheme or "http").lower()
    port = parts.port or (443 if scheme == "https" else 80)
    from transport.proxy import ProxyConfig

    return ProxyConfig(
        scheme=scheme,
        host=parts.hostname,
        port=port,
        username=parts.username,
        password=parts.password,
    )

