"""AnonymousVertexProvider — node-aware Google upstream adapter (ANON-004).

Implements the Anonymous Vertex / Agent Platform batchGraphql protocol
through a layered protocol stack:

    AnonymousVertexProvider
            |
            v
    AnonymousVertexNodePool      (nodes.py: acquire / lease / record_*)
            |
            v
    ExecutionNode (per node: own transport + recaptcha + client)
            |
            v
    AnonymousVertexClient   (client.py)
            |
            v
    AnonymousVertexProtocol (protocol.py)
            |
            v
    HTTP Transport          (transport.py)

Attempt model (one request = one attempt = ONE node):

    chat request
      -> NodePool.acquire()          (lease)
      -> reCAPTCHA token via THE SAME node
      -> batchGraphql via THE SAME node
      -> record success / 429 / transport failure (node-level)
      -> lease release (finally; also on cancellation / mid-stream death)

A failing attempt never switches node mid-flight: the node is recorded and
released, and the next attempt re-acquires (a different) node.  Once a
stream has yielded a chunk to the gateway, an error is raised immediately
— never retried transparently (no duplicate output).

The Provider never builds GraphQL payloads or Google headers itself
(TASK-002-A section 18); node-level 429s and reCAPTCHA failures never
touch the core ResourcePool / scheduler health.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, List, Optional

from core.errors import (
    ProviderError,
    RateLimitError,
    UpstreamUnavailableError,
)
from core.health import HealthResult, HealthState
from core.models import ChatChunk, ChatRequest, ChatResponse, ModelInfo
from core.provider import Provider
from core.resource import Resource

from providers.anonymous_vertex.admission_orchestrator import (
    AdmissionOrchestrator,
)
from providers.anonymous_vertex.admission_scheduler import AdmissionScheduler
from providers.anonymous_vertex.admission_sink import AdmissionResultSink
from providers.anonymous_vertex.admission_store import (
    InMemoryAdmissionResultStore,
)

from providers.anonymous_vertex.node_definitions import NodeDefinition
from providers.anonymous_vertex.nodes import (
    AdmissionProjection,
    AnonymousVertexNodePool,
    CooldownPolicy,
    ExecutionNode,
    NodeLease,
    NodeSpec,
)
from providers.anonymous_vertex.request import chat_to_vertex_request

# Compat re-export (historical: tests import the resource type from here).
from providers.anonymous_vertex.resource import AnonymousVertexResource  # noqa: F401

from providers.anonymous_vertex.response import (
    map_finish_reason,
    vertex_chunk_to_chat_chunk,
    vertex_response_to_chat_response,
)
from providers.anonymous_vertex.streaming import (
    chunk_finish_reason,
    normalize_chunk,
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
    "gemini-3.8-flash",
]

#: Per-request node-attempt budget (each attempt binds exactly one node).
DEFAULT_MAX_NODE_ATTEMPTS = 3

#: Admission re-check interval / concurrency defaults (ANON-012).
DEFAULT_ADMISSION_INTERVAL_SECONDS = 300.0
DEFAULT_ADMISSION_MAX_CONCURRENCY = 5


class _RecaptchaAttemptFailure(Exception):
    """Internal: the reCAPTCHA step of this attempt failed (node recorded)."""

    def __init__(self, provider_error: UpstreamUnavailableError) -> None:
        self.provider_error = provider_error
        super().__init__(str(provider_error))


class AnonymousVertexProvider(Provider):
    """Adapter for the Anonymous Vertex batchGraphql endpoint."""

    def __init__(
        self,
        *,
        api_key: str = "",
        http_client: Optional[Any] = None,
        token_fetcher=None,
        models: Optional[List[str]] = None,
        node_specs: Optional[List[NodeSpec]] = None,
        node_definitions: Optional[List[NodeDefinition]] = None,
        node_pool: Optional[AnonymousVertexNodePool] = None,
        cooldown_policy: Optional[CooldownPolicy] = None,
        max_node_attempts: int = DEFAULT_MAX_NODE_ATTEMPTS,
        admission_orchestrator: Optional[AdmissionOrchestrator] = None,
        admission_interval_seconds: float = (
            DEFAULT_ADMISSION_INTERVAL_SECONDS
        ),
        admission_max_concurrency: int = DEFAULT_ADMISSION_MAX_CONCURRENCY,
    ) -> None:
        self.api_key = api_key or ANON_API_KEY
        self._token_fetcher = token_fetcher
        self._models = list(models) if models else list(TEXT_MODELS)
        self._max_node_attempts = max(1, int(max_node_attempts))
        self._admission_start_lock: Optional[asyncio.Lock] = None
        self._admission_started = False
        self._admission_stopped = False
        self._admission_clients: dict = {}

        if node_pool is not None:
            # Test-only injection: a caller-built pool runs in legacy mode
            # (no admission projection) and owns its own wiring.
            self.node_pool = node_pool
            self.node_definitions: List[NodeDefinition] = []
            self.admission_projection = None
            self.admission_store = None
            self.admission_sink = None
            self._admission_scheduler = None
            self._admission_orchestrator = admission_orchestrator
            return

        if node_definitions is not None:
            # Production construction path (ANON-012): ExecutionNodes AND
            # the AdmissionScheduler are built from the SAME
            # NodeDefinition objects — one store, one projection, one
            # sink, one scheduler per provider instance.
            self.node_definitions = list(node_definitions)
            self.admission_projection = AdmissionProjection()
            self.admission_store = InMemoryAdmissionResultStore()
            nodes = [
                ExecutionNode.from_definition(
                    definition, api_key=self.api_key
                )
                for definition in self.node_definitions
            ]
            self.node_pool = AnonymousVertexNodePool(
                nodes,
                policy=cooldown_policy,
                admission_projection=self.admission_projection,
            )
            self.admission_sink = AdmissionResultSink(
                self.admission_store, self.node_pool
            )
            self._admission_orchestrator = (
                admission_orchestrator
                if admission_orchestrator is not None
                else self._build_default_admission_orchestrator()
            )
            self._admission_interval_seconds = float(
                admission_interval_seconds
            )
            self._admission_max_concurrency = int(admission_max_concurrency)
            # The scheduler OBJECT is created at start_admission() so it
            # binds the orchestrator current at start time (wire -> start,
            # ANON-012 section 6); everything it needs already exists.
            self._admission_scheduler = None
            return

        # Legacy / test construction (NodeSpec or bare default): no
        # admission stack, behaviour identical to pre-ANON-012 providers.
        self.node_definitions = []
        self.admission_projection = None
        self.admission_store = None
        self.admission_sink = None
        self._admission_scheduler = None
        self._admission_orchestrator = None
        self.node_pool = AnonymousVertexNodePool.from_specs(
            node_specs or [NodeSpec(node_id="default")],
            api_key=self.api_key,
            policy=cooldown_policy,
            default_http_client=http_client,
        )

    def _admission_client_for_node(self, node: NodeDefinition) -> Any:
        """One probe client PER NODE, using the node's REAL egress.

        The node's own proxy configuration (socks5/http/https) becomes the
        client's outbound proxy; a direct node gets a proxy-less client.
        The probe then targets the fixed probe endpoints THROUGH that
        egress — the proxy config is never misread as a target URL.
        Cached per node_id; closed by :meth:`close`.
        """
        node_id = node.node_id
        client = self._admission_clients.get(node_id)
        if client is None:
            from transport.http import build_client
            from transport.proxy import ProxyConfig, TransportConfig

            proxy = node.proxy or {}
            scheme = str(proxy.get("scheme") or "direct").strip().lower()
            proxy_config = None
            if scheme != "direct" and proxy.get("host"):
                proxy_config = ProxyConfig(
                    scheme=scheme,
                    host=proxy.get("host"),
                    port=proxy.get("port"),
                    username=proxy.get("username"),
                    password=proxy.get("password"),
                )
            client = build_client(
                TransportConfig(proxy=proxy_config, timeout_seconds=30.0)
            )
            self._admission_clients[node_id] = client
        return client

    def _build_default_admission_orchestrator(self) -> AdmissionOrchestrator:
        """Real checker pipeline; every probe goes through the probed
        node's own real transport via the per-node client factory."""
        from providers.anonymous_vertex.checkers import (
            AnonymousVertexAuthChecker,
            AnonymousVertexCapabilityChecker,
            AnonymousVertexConnectivityChecker,
        )

        return AdmissionOrchestrator([
            AnonymousVertexConnectivityChecker(
                client_factory=self._admission_client_for_node
            ),
            AnonymousVertexCapabilityChecker(
                client_factory=self._admission_client_for_node
            ),
            AnonymousVertexAuthChecker(
                client_factory=self._admission_client_for_node
            ),
        ])

    @property
    def admission_scheduler(self) -> Optional[AdmissionScheduler]:
        return self._admission_scheduler

    @property
    def admission_running(self) -> bool:
        if self._admission_scheduler is None:
            return False
        return self._admission_scheduler.running

    async def start_admission(self) -> None:
        """Start the background admission scheduler (idempotent).

        Called by the application lifespan on startup; safe to call again.
        After :meth:`close` the scheduler is never restarted on this
        provider instance.
        """
        if self._admission_orchestrator is None:
            return  # legacy provider: no admission stack to start
        if self._admission_started:
            return
        if self._admission_start_lock is None:
            self._admission_start_lock = asyncio.Lock()
        async with self._admission_start_lock:
            if self._admission_started or self._admission_stopped:
                return
            self._admission_scheduler = AdmissionScheduler(
                self._admission_orchestrator,
                self.node_definitions,
                interval_seconds=self._admission_interval_seconds,
                max_concurrency=self._admission_max_concurrency,
                result_sink=self.admission_sink,
            )
            self._admission_started = True
            await self._admission_scheduler.start()

    # -- lifecycle / compat wiring (tests) --

    def set_http_client(self, client: Any) -> None:
        """Inject an httpx.AsyncClient-compatible object as the default
        node's transport (tests)."""
        self.node_pool.nodes[0].set_http_client(client)

    def set_token_fetcher(self, fetcher) -> None:
        """Inject a recaptcha token fetcher (tests); takes precedence over
        the per-node real flow."""
        self._token_fetcher = fetcher

    async def close(self) -> None:
        """Stop the admission scheduler (no residual background tasks) and
        close every node transport.  Idempotent."""
        self._admission_stopped = True
        if self._admission_scheduler is not None:
            await self._admission_scheduler.stop()
        for client in self._admission_clients.values():
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001 - shutdown best-effort
                    pass
        self._admission_clients.clear()
        for node in self.node_pool.nodes:
            await node.close()

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

    # -- node-aware attempt machinery --

    async def _fetch_token(self, lease: NodeLease, resource: Resource) -> str:
        """Fetch a fresh recaptcha token THROUGH THE LEASED NODE.

        The per-node real flow uses the node's own transport, so the
        reCAPTCHA and the following batchGraphql call share one fixed
        outbound path.  Outcome is recorded on the node (never on the
        core resource pool).
        """
        if self._token_fetcher is not None:
            return await self._token_fetcher(resource)
        from providers.anonymous_vertex.recaptcha import fetch_recaptcha_token

        node = lease.node
        try:
            http = await node.http_client()
            token = await fetch_recaptcha_token(client=http)
        except Exception as exc:
            raise _RecaptchaAttemptFailure(
                UpstreamUnavailableError(
                    f"recaptcha token fetch failed: {exc}",
                    provider="anonymous_vertex",
                )
            ) from exc
        await self.node_pool.record_recaptcha_success(node.node_id)
        return token

    async def _collect_response(
        self, node: ExecutionNode, vertex_request, token: str, request: ChatRequest
    ) -> ChatResponse:
        """Non-streaming: run one node's upstream stream and collect it."""
        client = await node.vertex_client()

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

    async def _stream_chunks(
        self, node: ExecutionNode, vertex_request, token: str, model: str
    ) -> AsyncIterator[ChatChunk]:
        """Streaming from one node's upstream NDJSON frames."""
        client = await node.vertex_client()

        usage_meta: Optional[dict] = None
        model_version = model
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

    async def complete(
        self, request: ChatRequest, resource: Resource
    ) -> ChatResponse:
        """Non-streaming completion through the node pool.

        Each attempt binds ONE node (reCAPTCHA + batchGraphql on the same
        outbound path).  A failed attempt records the node and the next
        attempt re-acquires; exhausted attempts re-raise the last error.
        """
        vertex_request = chat_to_vertex_request(request)
        last_error: Optional[ProviderError] = None
        tried: set[str] = set()

        for _ in range(self._max_node_attempts):
            # Work-conserving (ANON-005): when healthy untried nodes exist
            # but are all at max_concurrency, wait for a release instead of
            # failing the request.  Cooldowns / disabled / recaptcha-failed
            # nodes never block the call.
            lease = await self.node_pool.acquire(
                skip=tried, wait_for_capacity=True
            )
            if lease is None:
                break
            tried.add(lease.node_id)
            try:
                token = await self._fetch_token(lease, resource)
                started = time.monotonic()
                response = await self._collect_response(
                    lease.node, vertex_request, token, request
                )
                await self.node_pool.record_success(
                    lease.node_id, latency_s=time.monotonic() - started
                )
                return response
            except _RecaptchaAttemptFailure as exc:
                await self.node_pool.record_recaptcha_failure(
                    lease.node_id, str(exc)
                )
                last_error = exc.provider_error
            except RateLimitError as exc:
                await self.node_pool.record_rate_limit(lease.node_id, exc.retry_after)
                last_error = exc
            except ProviderError as exc:
                await self.node_pool.record_failure(lease.node_id, exc)
                last_error = exc
            except Exception as exc:  # noqa: BLE001 - never leak raw errors
                await self.node_pool.record_failure(lease.node_id, exc)
                last_error = UpstreamUnavailableError(
                    str(exc), provider="anonymous_vertex"
                )
            finally:
                await lease.release()

        if last_error is not None:
            raise last_error
        raise UpstreamUnavailableError(
            "no execution node currently available", provider="anonymous_vertex"
        )

    async def stream(
        self, request: ChatRequest, resource: Resource
    ) -> AsyncIterator[ChatChunk]:
        """Streaming completion through the node pool.

        Same one-node-per-attempt contract as :meth:`complete`.  Once any
        chunk has been yielded to the gateway, errors propagate
        immediately — no transparent retry could ever duplicate output.
        """
        vertex_request = chat_to_vertex_request(request)
        last_error: Optional[ProviderError] = None
        tried: set[str] = set()

        for _ in range(self._max_node_attempts):
            # Work-conserving (ANON-005): when healthy untried nodes exist
            # but are all at max_concurrency, wait for a release instead of
            # failing the request.  Cooldowns / disabled / recaptcha-failed
            # nodes never block the call.
            lease = await self.node_pool.acquire(
                skip=tried, wait_for_capacity=True
            )
            if lease is None:
                break
            tried.add(lease.node_id)
            sent_any = False
            try:
                token = await self._fetch_token(lease, resource)
                started = time.monotonic()
                async for chunk in self._stream_chunks(
                    lease.node, vertex_request, token, request.model
                ):
                    sent_any = True
                    yield chunk
                await self.node_pool.record_success(
                    lease.node_id, latency_s=time.monotonic() - started
                )
                return
            except _RecaptchaAttemptFailure as exc:
                await self.node_pool.record_recaptcha_failure(
                    lease.node_id, str(exc)
                )
                last_error = exc.provider_error
            except RateLimitError as exc:
                await self.node_pool.record_rate_limit(lease.node_id, exc.retry_after)
                last_error = exc
                if sent_any:
                    raise
            except ProviderError as exc:
                await self.node_pool.record_failure(lease.node_id, exc)
                last_error = exc
                if sent_any:
                    raise
            except Exception as exc:  # noqa: BLE001 - never leak raw errors
                await self.node_pool.record_failure(lease.node_id, exc)
                last_error = UpstreamUnavailableError(
                    str(exc), provider="anonymous_vertex"
                )
                if sent_any:
                    raise
            finally:
                await lease.release()

        if last_error is not None:
            raise last_error
        raise UpstreamUnavailableError(
            "no execution node currently available", provider="anonymous_vertex"
        )


def trim(model: str) -> str:
    from providers.anonymous_vertex.signature import trim_gemini_path_prefix

    return trim_gemini_path_prefix(model)


def _flatten(norm: Any) -> List[Any]:
    """Flatten a normalized chunk (which may be a list of items) to a list."""
    if isinstance(norm, list):
        return norm
    return [norm]
