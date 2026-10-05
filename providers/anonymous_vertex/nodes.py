"""Execution node pool for Anonymous Vertex (ANON-004).

A Node is ONE complete, fixed outbound execution path — its own transport
(with its own proxy) serving BOTH the reCAPTCHA token fetch and the
batchGraphql call.  A request is bound to one node for its whole attempt;
nodes are never shared mid-attempt.

    AnonymousVertexProvider
            |
            v
    AnonymousVertexNodePool
            |
    +-------+--------+
    v                v
Node A            Node B
  own http client   own http client
  own recaptcha     own recaptcha
  own batchGraphql  own batchGraphql

Selection policy (v1, deliberately simple):

    eligible nodes (enabled, not cooling down, recaptcha-admitted,
    below max_concurrency)
        -> lowest current_in_flight
        -> higher weight
        -> stable registration order

This module owns NODE-level runtime state only.  It never touches the
core ResourcePool / scheduler health: a node-level 429 or a reCAPTCHA
failure cools the node, not the provider or the resource.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from providers.anonymous_vertex.client import AnonymousVertexClient
from providers.anonymous_vertex.node_definitions import (
    NodeDefinition,
    NodeRuntimeState,
)
from providers.anonymous_vertex.transport import HttpxTransport


@dataclass
class NodeSpec:
    """Compatibility / construction DTO for one execution node.

    ``NodeSpec`` is how the factory and tests hand static configuration to
    an ExecutionNode; it is NOT runtime state (no counters live here) and
    NOT the persistable boundary object — that is
    :class:`providers.anonymous_vertex.node_definitions.NodeDefinition`.
    """

    node_id: str
    proxy: Dict[str, Any] = field(default_factory=dict)
    weight: int = 1
    max_concurrency: int = 8
    enabled: bool = True


class CooldownPolicy:
    """Node-level cooldown budget (all values in seconds; configurable).

    429s escalate 30s -> 60s -> 120s -> ... by default; reCAPTCHA failures
    and repeated transport failures have their own ladders.  An upstream
    Retry-After is honoured but never below the escalating floor.
    """

    def __init__(
        self,
        *,
        rate_limit_base: float = 30.0,
        rate_limit_factor: float = 2.0,
        rate_limit_max: float = 600.0,
        recaptcha_base: float = 60.0,
        recaptcha_factor: float = 2.0,
        recaptcha_max: float = 900.0,
        failure_threshold: int = 3,
        failure_base: float = 20.0,
        failure_factor: float = 2.0,
        failure_max: float = 300.0,
    ) -> None:
        self.rate_limit_base = rate_limit_base
        self.rate_limit_factor = rate_limit_factor
        self.rate_limit_max = rate_limit_max
        self.recaptcha_base = recaptcha_base
        self.recaptcha_factor = recaptcha_factor
        self.recaptcha_max = recaptcha_max
        self.failure_threshold = failure_threshold
        self.failure_base = failure_base
        self.failure_factor = failure_factor
        self.failure_max = failure_max

    @staticmethod
    def _escalate(base: float, factor: float, maximum: float, n: int) -> float:
        """base * factor^(n-1), capped; n >= 1 (the n-th consecutive hit)."""
        if n < 1:
            n = 1
        return min(maximum, base * (factor ** (n - 1)))

    def rate_limit_delay(self, consecutive: int, retry_after: Optional[float]) -> float:
        delay = self._escalate(
            self.rate_limit_base, self.rate_limit_factor, self.rate_limit_max,
            consecutive,
        )
        if retry_after is not None and retry_after > 0:
            delay = max(delay, float(retry_after))
        return delay

    def recaptcha_delay(self, consecutive: int) -> float:
        return self._escalate(
            self.recaptcha_base, self.recaptcha_factor, self.recaptcha_max,
            consecutive,
        )

    def failure_delay(self, consecutive: int) -> float:
        return self._escalate(
            self.failure_base, self.failure_factor, self.failure_max,
            consecutive,
        )


class ExecutionNode:
    """One execution node: identity, runtime state and its own transport.

    The node lazily builds and caches its own HTTP client (with the node's
    proxy) and its own AnonymousVertexClient.  Injected clients (tests) take
    precedence over the factory-built one.
    """

    def __init__(
        self,
        spec: NodeSpec,
        *,
        api_key: str = "",
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.spec = spec
        self.node_id = spec.node_id
        self.enabled = spec.enabled
        self.weight = spec.weight
        self.max_concurrency = spec.max_concurrency

        # -- scheduling state --
        self.current_in_flight: int = 0
        self.cooldown_until: float = 0.0  # time.monotonic() domain
        self.consecutive_rate_limits: int = 0
        self.consecutive_failures: int = 0
        self.recaptcha_state: str = "unknown"  # unknown | ok | failed

        # -- observability counters --
        self.total_requests: int = 0
        self.total_failures: int = 0
        self.total_rate_limits: int = 0
        self.recaptcha_passes: int = 0
        self.recaptcha_failures: int = 0
        self.last_latency_s: Optional[float] = None
        self.last_error: str = ""

        self._api_key = api_key
        self._client_factory = client_factory
        self._injected_http: Any = None
        self._http: Any = None
        self._client: Optional[AnonymousVertexClient] = None
        # Persistable identity (ANON-006), present when the node was built
        # from a NodeDefinition; runtime code never mutates it.
        self.definition: Optional["NodeDefinition"] = None

    # -- transport lifecycle (per node; never shared) --

    def set_http_client(self, client: Any) -> None:
        """Inject an httpx-compatible client (tests / compat entry point)."""
        self._injected_http = client
        self._http = None
        self._client = None

    async def http_client(self) -> Any:
        if self._http is not None:
            return self._http
        if self._injected_http is not None:
            self._http = self._injected_http
            return self._http
        if self._client_factory is not None:
            self._http = self._client_factory()
            return self._http
        from transport.http import build_client
        from transport.proxy import ProxyConfig, TransportConfig

        proxy = self.spec.proxy or {}
        cfg = TransportConfig(
            timeout_seconds=180.0,
            proxy=ProxyConfig(
                scheme=proxy.get("scheme", "direct"),
                host=proxy.get("host"),
                port=proxy.get("port"),
                username=proxy.get("username"),
                password=proxy.get("password"),
            ),
        )
        self._http = build_client(cfg)
        return self._http

    async def vertex_client(self) -> AnonymousVertexClient:
        """This node's own protocol client (cached per node)."""
        if self._client is None:
            self._client = AnonymousVertexClient(
                transport=HttpxTransport(await self.http_client()),
                api_key=self._api_key,
            )
        return self._client

    async def close(self) -> None:
        client = self._http
        self._http = None
        self._client = None
        if client is not None:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001 - shutdown best-effort
                    pass

    # -- state helpers --

    # -- definition / runtime-state boundary (ANON-006) --

    @classmethod
    def from_definition(
        cls,
        definition: "NodeDefinition",
        *,
        api_key: str = "",
        client_factory: Optional[Callable[[], Any]] = None,
        runtime_state: Optional["NodeRuntimeState"] = None,
    ) -> "ExecutionNode":
        """Build a node from a persistable NodeDefinition.

        ``runtime_state`` is an explicit inheritance decision by the
        caller: pass one to carry counters over (e.g. an in-process
        reload), omit it for a fresh node.  The definition itself is kept
        by reference and is frozen — runtime mutations never touch it.
        """
        spec = NodeSpec(
            node_id=definition.node_id,
            proxy=dict(definition.proxy),
            weight=definition.weight,
            max_concurrency=definition.max_concurrency,
            enabled=definition.enabled,
        )
        node = cls(spec, api_key=api_key, client_factory=client_factory)
        node.definition = definition
        if runtime_state is not None:
            runtime_state.apply_to(node)
        return node

    def capture_runtime_state(self) -> "NodeRuntimeState":
        """Snapshot this node's discardable runtime state."""
        return NodeRuntimeState.capture(self)

    def restore_runtime_state(self, state: "NodeRuntimeState") -> None:
        """Apply a runtime-state snapshot (caller decides inheritance)."""
        state.apply_to(self)

    def in_cooldown(self, now: float) -> bool:
        return now < self.cooldown_until

    @property
    def health(self) -> str:
        if not self.enabled:
            return "DISABLED"
        if self.in_cooldown(time.monotonic()):
            return "COOLDOWN"
        return "HEALTHY"

    def snapshot(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "enabled": self.enabled,
            "weight": self.weight,
            "max_concurrency": self.max_concurrency,
            "current_in_flight": self.current_in_flight,
            "health": self.health,
            "cooldown_until": self.cooldown_until,
            "consecutive_rate_limits": self.consecutive_rate_limits,
            "consecutive_failures": self.consecutive_failures,
            "recaptcha_state": self.recaptcha_state,
            "recaptcha_passes": self.recaptcha_passes,
            "recaptcha_failures": self.recaptcha_failures,
            "total_requests": self.total_requests,
            "total_failures": self.total_failures,
            "total_rate_limits": self.total_rate_limits,
            "last_latency_s": self.last_latency_s,
            "last_error": self.last_error,
        }


class NodeLease:
    """One acquisition of a node.  Must be released exactly once; the
    Provider releases it in a ``finally`` so exceptions, cancellations and
    mid-stream disconnects cannot leak the in-flight count."""

    __slots__ = ("_pool", "_node", "acquired_at", "released")

    def __init__(self, pool: "AnonymousVertexNodePool", node: ExecutionNode) -> None:
        self._pool = pool
        self._node = node
        self.acquired_at = pool.now()
        self.released = False

    @property
    def node(self) -> ExecutionNode:
        return self._node

    @property
    def node_id(self) -> str:
        return self._node.node_id

    async def release(self) -> None:
        await self._pool.release(self)

    async def __aenter__(self) -> "NodeLease":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release()


class AnonymousVertexNodePool:
    """Scheduler for execution nodes (acquire / release / record_*)."""

    def __init__(
        self,
        nodes: List[ExecutionNode],
        *,
        policy: Optional[CooldownPolicy] = None,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        if not nodes:
            raise ValueError("node pool requires at least one node")
        self._nodes = list(nodes)
        self._policy = policy or CooldownPolicy()
        self.now = now_fn or time.monotonic
        self._lock = asyncio.Lock()
        # Shares the pool lock: notify_all() is legal from any block that
        # holds self._lock (release / record_*).
        self._condition = asyncio.Condition(lock=self._lock)

    @classmethod
    def from_specs(
        cls,
        specs: List[NodeSpec],
        *,
        api_key: str = "",
        policy: Optional[CooldownPolicy] = None,
        now_fn: Optional[Callable[[], float]] = None,
        default_http_client: Any = None,
    ) -> "AnonymousVertexNodePool":
        """Build a pool from static specs.

        ``default_http_client`` (compat/test injection) is wired into the
        FIRST node; every node still owns its own transport lifecycle.
        """
        nodes = []
        for i, spec in enumerate(specs):
            factory = None
            if i == 0 and default_http_client is not None:
                node = ExecutionNode(spec, api_key=api_key)
                node.set_http_client(default_http_client)
                nodes.append(node)
                continue
            nodes.append(ExecutionNode(spec, api_key=api_key, client_factory=factory))
        return cls(nodes, policy=policy, now_fn=now_fn)

    # -- introspection --

    @property
    def nodes(self) -> List[ExecutionNode]:
        return list(self._nodes)

    def node(self, node_id: str) -> ExecutionNode:
        for n in self._nodes:
            if n.node_id == node_id:
                return n
        raise KeyError(f"unknown node: {node_id}")

    def snapshot(self) -> List[Dict[str, Any]]:
        return [n.snapshot() for n in self._nodes]

    async def set_node_enabled(self, node_id: str, enabled: bool) -> None:
        """Enable/disable a node under the pool lock (ANON-005-FIX-01).

        Enable/disable changes scheduler-visible eligibility, so it shares
        the pool lock with acquire/release and wakes capacity waiters via
        ``notify_all()`` — no lost notifications from concurrent
        modifications, and waiters re-check the full candidate set."""
        async with self._lock:
            self.node(node_id).enabled = enabled
            self._notify_state_change()

    # -- acquire / release --

    def _refresh_state(self, node: ExecutionNode, now: float) -> None:
        """Lazy cooldown expiry: once the cooldown has passed the node is
        schedulable again and a previous recaptcha failure gets one fresh
        evaluation on its next attempt."""
        if node.cooldown_until and now >= node.cooldown_until:
            node.cooldown_until = 0.0
            if node.recaptcha_state == "failed":
                node.recaptcha_state = "unknown"

    def _healthy(self, node: ExecutionNode, now: float) -> bool:
        """Enabled, not cooling down, reCAPTCHA-admitted — capacity NOT
        considered.  Capacity-blocked healthy nodes are exactly what the
        work-conserving acquire waits for."""
        self._refresh_state(node, now)
        if not node.enabled:
            return False
        if node.in_cooldown(now):
            return False
        if node.recaptcha_state == "failed":
            return False
        return True

    def _has_capacity(self, node: ExecutionNode) -> bool:
        return node.current_in_flight < node.max_concurrency

    def _pick(self, candidates: List[ExecutionNode]) -> ExecutionNode:
        return min(
            candidates,
            key=lambda n: (n.current_in_flight, -n.weight),
        )

    async def acquire(
        self,
        *,
        skip: Optional[set] = None,
        wait_for_capacity: bool = False,
    ) -> Optional[NodeLease]:
        """Acquire one eligible node as a lease, or None when no healthy
        candidate exists.

        ``skip`` excludes node ids already tried within the current
        request, so a failed node cannot be re-picked by the next attempt
        (mirrors the core scheduler's ``tried`` semantics).  When every
        healthy candidate is already skipped, an already-tried node may be
        reused (ANON-004 "next attempt" semantics).

        Work-conserving (ANON-005): with ``wait_for_capacity=True`` the
        call WAITS when healthy untried nodes exist but are all at
        ``max_concurrency`` — it never waits for cooldowns, disabled or
        reCAPTCHA-failed nodes, and never returns while execution capacity
        is idle.  Every state change (release, cooldowns, admission)
        notifies waiters, which re-check the full predicate under the
        pool lock; ``max_concurrency`` can never be exceeded.
        """
        skip = skip or frozenset()
        async with self._condition:  # holds self._lock
            while True:
                now = self.now()
                healthy = [
                    n for n in self._nodes if self._healthy(n, now)
                ]
                untried = [n for n in healthy if n.node_id not in skip]
                tried = [n for n in healthy if n.node_id in skip]

                ready_untried = [n for n in untried if self._has_capacity(n)]
                if ready_untried:
                    node = self._pick(ready_untried)
                    node.current_in_flight += 1
                    return NodeLease(self, node)

                if untried and wait_for_capacity:
                    # Healthy untried nodes exist but are capacity-blocked:
                    # wait for a release / state change instead of failing.
                    await self._condition.wait()
                    continue

                # ANON-004 reuse fallback: every healthy candidate is
                # already tried (or no untried was ready and we are not
                # waiting) — reuse a tried node rather than giving up.
                ready_tried = [n for n in tried if self._has_capacity(n)]
                if ready_tried:
                    node = self._pick(ready_tried)
                    node.current_in_flight += 1
                    return NodeLease(self, node)

                if tried and wait_for_capacity:
                    # Only already-tried nodes exist and all are at
                    # capacity: wait, then reuse the first freed one.
                    await self._condition.wait()
                    continue

                return None

    async def release(self, lease: NodeLease) -> None:
        """Release a lease exactly once (idempotent).

        The ``released`` flag is checked and flipped under the pool lock,
        so a double release — explicit + async-context-manager exit, or a
        race between two callers — decrements ``in_flight`` only once.
        Wakes capacity waiters (they re-check full eligibility).
        """
        async with self._lock:
            if lease.released:
                return
            lease.released = True
            node = lease._node
            node.current_in_flight = max(0, node.current_in_flight - 1)
            self._condition.notify_all()

    def _notify_state_change(self) -> None:
        """Wake capacity waiters after any scheduling-relevant state
        change (cooldown applied/cleared, admission result).  Callers must
        hold the pool lock."""
        self._condition.notify_all()

    # -- outcome recording --

    async def record_success(
        self, node_id: str, latency_s: Optional[float] = None
    ) -> None:
        async with self._lock:
            node = self.node(node_id)
            node.total_requests += 1
            node.consecutive_rate_limits = 0
            node.consecutive_failures = 0
            node.cooldown_until = 0.0
            if node.recaptcha_state != "failed":
                node.recaptcha_state = "ok"
            node.last_latency_s = latency_s
            self._notify_state_change()

    async def record_rate_limit(
        self, node_id: str, retry_after: Optional[float] = None
    ) -> None:
        """429 -> THIS node cools down (escalating); the provider as a
        whole stays schedulable on its other nodes."""
        async with self._lock:
            node = self.node(node_id)
            node.total_requests += 1
            node.total_failures += 1
            node.total_rate_limits += 1
            node.consecutive_rate_limits += 1
            delay = self._policy.rate_limit_delay(
                node.consecutive_rate_limits, retry_after
            )
            node.cooldown_until = self.now() + delay
            node.last_error = "rate limited"
            self._notify_state_change()

    async def record_failure(self, node_id: str, error: Any) -> None:
        """Transport / application failure -> counters; cooldown only once
        failures repeat (threshold, escalating)."""
        async with self._lock:
            node = self.node(node_id)
            node.total_requests += 1
            node.total_failures += 1
            node.consecutive_failures += 1
            node.last_error = str(error)[:200]
            if node.consecutive_failures >= self._policy.failure_threshold:
                delay = self._policy.failure_delay(node.consecutive_failures)
                node.cooldown_until = self.now() + delay
            self._notify_state_change()

    async def record_recaptcha_success(self, node_id: str) -> None:
        """A fresh token proves the node's egress path again: clears any
        recaptcha failure state and its cooldown."""
        async with self._lock:
            node = self.node(node_id)
            node.recaptcha_passes += 1
            node.recaptcha_state = "ok"
            node.cooldown_until = 0.0
            self._notify_state_change()

    async def record_recaptcha_failure(self, node_id: str, error: Any = "") -> None:
        """Token fetch failed -> node unschedulable for the recaptcha
        cooldown window (NODE-level only; never the provider's core
        resource health)."""
        async with self._lock:
            node = self.node(node_id)
            node.recaptcha_failures += 1
            node.recaptcha_state = "failed"
            node.total_failures += 1
            node.consecutive_failures += 1
            node.last_error = f"recaptcha failure: {str(error)[:150]}"
            delay = self._policy.recaptcha_delay(node.recaptcha_failures)
            node.cooldown_until = self.now() + delay
            self._notify_state_change()
