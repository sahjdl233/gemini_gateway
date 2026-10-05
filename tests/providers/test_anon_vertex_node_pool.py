"""ANON-004 acceptance tests: Anonymous Vertex execution node pool."""

from __future__ import annotations

import asyncio
import json

import pytest

from core.errors import RateLimitError, UpstreamUnavailableError
from core.models import ChatRequest, ChatMessage
from providers.anonymous_vertex.nodes import (
    AnonymousVertexNodePool,
    CooldownPolicy,
    ExecutionNode,
    NodeSpec,
)
from providers.anonymous_vertex.provider import AnonymousVertexProvider

from tests.providers.test_anonymous_vertex import (
    _FRAME1,
    _FRAME1_TEXT,
    _FRAME2,
    _StaticStreamResponse,
    _StreamResponseFake,
    make_request,
    make_resource,
)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _node(node_id="default", *, max_concurrency=8, weight=1, enabled=True,
          client=None):
    node = ExecutionNode(
        NodeSpec(node_id=node_id, weight=weight,
                 max_concurrency=max_concurrency, enabled=enabled)
    )
    if client is not None:
        node.set_http_client(client)
    return node


def _pool(*nodes, policy=None):
    clock = FakeClock()
    pool = AnonymousVertexNodePool(list(nodes), policy=policy, now_fn=clock)
    pool.clock = clock
    return pool


async def _stub_token(resource):
    return "recaptcha-token"


def _provider(pool, *, token_fetcher=_stub_token):
    # token_fetcher=None exercises the real per-node recaptcha flow (fakes
    # must then implement anchor/reload); the stub keeps node-level tests
    # focused on scheduling.
    return AnonymousVertexProvider(node_pool=pool, token_fetcher=token_fetcher)


# ---------------------------------------------------------------------------
# 1. single node acquire / release
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_single_node_acquire_release():
    node = _node()
    pool = _pool(node)
    lease = await pool.acquire()
    assert lease is not None
    assert lease.node is node
    assert node.current_in_flight == 1
    await lease.release()
    assert node.current_in_flight == 0

    # lease doubles as an async context manager
    async with await pool.acquire() as lease2:
        assert node.current_in_flight == 1
    assert node.current_in_flight == 0


# ---------------------------------------------------------------------------
# 2. multi-node least-in-flight selection (weight / stable tie-break)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_least_in_flight_selection_with_weight_tiebreak():
    a, b, c = _node("a"), _node("b", weight=5), _node("c")
    pool = _pool(a, b, c)
    # all idle: weight wins the tie (b), then stable order (a before c)
    l1 = await pool.acquire()
    assert l1.node_id == "b"
    # b now carries 1 in-flight: least-in-flight beats weight -> a (stable
    # over c), then c, then the round comes back to b
    l2 = await pool.acquire()
    assert l2.node_id == "a"
    l3 = await pool.acquire()
    assert l3.node_id == "c"
    l4 = await pool.acquire()
    assert l4.node_id == "b"
    for lease in (l1, l2, l3, l4):
        await lease.release()
    assert a.current_in_flight == b.current_in_flight == c.current_in_flight == 0


# ---------------------------------------------------------------------------
# 3. max_concurrency is never exceeded
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_max_concurrency_not_exceeded():
    a = _node("a", max_concurrency=1)
    b = _node("b", max_concurrency=1)
    pool = _pool(a, b)
    la = await pool.acquire()
    lb = await pool.acquire()
    assert la.node_id == "a" and lb.node_id == "b"
    assert (await pool.acquire()) is None  # every node at capacity
    assert a.current_in_flight == 1 and b.current_in_flight == 1
    await la.release()
    await lb.release()


# ---------------------------------------------------------------------------
# 4. release happens on exception and cancellation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_release_on_exception_and_cancellation():
    import httpx

    from providers.anonymous_vertex.errors import AnonymousVertexConnectionError

    resp = _StreamResponseFake()
    resp.push(_FRAME1)
    resp.push(httpx.ReadError("reset mid-stream"))
    provider = _provider(_pool(_node("only", client=_StreamRespClient(resp))))

    request = make_request()
    with pytest.raises(AnonymousVertexConnectionError):
        async for _ in provider.stream(request, make_resource()):
            pass
    assert provider.node_pool.nodes[0].current_in_flight == 0

    # cancellation while parked mid-stream
    resp2 = _StreamResponseFake()  # never pushes anything
    provider.set_http_client(_StreamRespClient(resp2))
    task = asyncio.create_task(_consume_all(provider, request))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.node_pool.nodes[0].current_in_flight == 0


async def _consume_all(provider, request):
    async for _ in provider.stream(request, make_resource()):
        pass


class _StreamRespClient:
    """httpx-shaped fake client always streaming the given response."""

    def __init__(self, response):
        self._response = response

    def stream(self, method, url, *, content=None, headers=None):
        from contextlib import asynccontextmanager

        resp = self._response

        @asynccontextmanager
        async def cm():
            yield resp

        return cm()


# ---------------------------------------------------------------------------
# 5 + 7. 429 -> node cooldown (escalating); expiry re-eligibles the node
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rate_limit_cools_node_and_recovers_after_expiry():
    node = _node("only")
    pool = _pool(node)
    await pool.record_rate_limit("only")  # 1st: 30s
    assert node.cooldown_until - pool.clock.t == pytest.approx(30.0)
    assert (await pool.acquire()) is None

    # second consecutive 429 -> 60s; third -> 120s
    await pool.record_rate_limit("only")
    assert node.consecutive_rate_limits == 2
    assert node.cooldown_until - pool.clock.t == pytest.approx(60.0)
    await pool.record_rate_limit("only")
    assert node.cooldown_until - pool.clock.t == pytest.approx(120.0)

    # expiry -> eligible again
    pool.clock.advance(121.0)
    lease = await pool.acquire()
    assert lease is not None and lease.node_id == "only"
    await lease.release()

    # success resets the escalation counter
    await pool.record_success("only")
    assert node.consecutive_rate_limits == 0


@pytest.mark.asyncio
async def test_rate_limit_honours_retry_after_floor():
    node = _node("only")
    pool = _pool(node)
    await pool.record_rate_limit("only", retry_after=7.0)
    # escalating floor (30s) wins over the smaller Retry-After
    assert node.cooldown_until - pool.clock.t == pytest.approx(30.0)
    await pool.record_rate_limit("only", retry_after=500.0)
    # bigger Retry-After wins over the 60s escalation
    assert node.cooldown_until - pool.clock.t == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# 6. cooling-down nodes are not acquired
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cooldown_node_not_acquired():
    a, b = _node("a"), _node("b")
    pool = _pool(a, b)
    await pool.record_rate_limit("a")
    lease = await pool.acquire()
    assert lease.node_id == "b"
    await lease.release()
    lease2 = await pool.acquire()
    assert lease2.node_id == "b"  # a still cooling
    await lease2.release()


# ---------------------------------------------------------------------------
# 8 + 9. reCAPTCHA admission: failure removes node, success restores it
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_recaptcha_failure_removes_node_until_success():
    a, b = _node("a"), _node("b")
    pool = _pool(a, b)
    await pool.record_recaptcha_failure("a")
    assert a.recaptcha_state == "failed"
    for _ in range(4):
        lease = await pool.acquire()
        assert lease.node_id == "b"  # never the recaptcha-failed node
        await lease.release()

    await pool.record_recaptcha_success("a")
    assert a.recaptcha_state == "ok"
    lease = await pool.acquire()
    assert lease.node_id in ("a", "b")  # a is back in the eligible set
    await lease.release()


@pytest.mark.asyncio
async def test_recaptcha_failed_node_recovers_after_cooldown():
    node = _node("only")
    pool = _pool(node)
    await pool.record_recaptcha_failure("only")
    assert (await pool.acquire()) is None
    pool.clock.advance(61.0)  # recaptcha base cooldown is 60s
    lease = await pool.acquire()
    assert lease is not None  # cooldown expiry gives one fresh evaluation
    await lease.release()


# ---------------------------------------------------------------------------
# 10. one attempt = one node for BOTH recaptcha and batchGraphql
# ---------------------------------------------------------------------------
class _NodeBoundClient:
    """Fake client that stamps every request with its node id and serves
    the real recaptcha anchor/reload flow plus a batchGraphql stream."""

    def __init__(self, node_id, registry):
        self.node_id = node_id
        self.registry = registry  # {"anchor": [...], "graphql": [...]}

    async def get(self, url, headers=None):
        # anchor GET + enterprise.js version probe: same node for both
        key = "version" if "enterprise.js" in url else "anchor"
        self.registry.setdefault(key, []).append(self.node_id)
        body = (
            '<html><input id="recaptcha-token" value="tok-'
            + self.node_id
            + '"></html>'
        )

        class _R:
            status_code = 200
            text = body

        return _R()

    async def post(self, url, content=None, headers=None):
        # recaptcha reload POST (urlencoded form) — same node again
        body = content if isinstance(content, bytes) else (content or "").encode()
        assert b"reason=q" in body
        self.registry.setdefault("reload", []).append(self.node_id)

        class _R:
            status_code = 200
            text = '[["rresp","tok-final"]]'

        return _R()

    def stream(self, method, url, *, content=None, headers=None):
        from contextlib import asynccontextmanager

        body = content or b""
        if b"recaptchaToken" in body:
            # batchGraphql call — record which node served it
            self.registry.setdefault("graphql", []).append(self.node_id)
            payload = {
                "results": [{
                    "data": {"ui": {"streamGenerateContentAnonymous": {
                        "candidates": [{"content": {"role": "model",
                                                    "parts": [{"text": "hi-" + self.node_id}]}}],
                    }}}
                }]
            }
            raw = json.dumps(payload).encode()
        else:
            # recaptcha reload POST
            self.registry.setdefault("reload", []).append(self.node_id)

        @asynccontextmanager
        async def cm():
            yield _StaticBodyResponse(200, raw)

        return cm()


class _StaticBodyResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.headers = {}
        self.closed = False
        self.aread_calls = 0

    async def aread(self):
        self.aread_calls += 1
        return self._body

    async def aclose(self):
        self.closed = True

    async def aiter_bytes(self):
        yield self._body


@pytest.mark.asyncio
async def test_recaptcha_and_graphql_use_the_same_node():
    registry: dict = {}
    nodes = [
        _node("a", client=_NodeBoundClient("a", registry)),
        _node("b", client=_NodeBoundClient("b", registry)),
    ]
    provider = _provider(_pool(*nodes), token_fetcher=None)
    resp = await provider.complete(make_request(), make_resource())
    assert resp.text == "hi-a"  # first attempt binds node a
    assert registry["anchor"] == ["a"]
    assert registry["version"] == ["a"]
    assert registry["reload"] == ["a"]
    assert registry["graphql"] == ["a"]  # same node as the recaptcha steps


# ---------------------------------------------------------------------------
# 11. a failed node -> next attempt picks another node; provider survives 429
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_node_failure_next_attempt_uses_other_node():
    dead = _StreamResponseFake()
    dead.push(_FRAME1[: len(_FRAME1) // 2])
    import httpx

    dead.push(httpx.ConnectError("reset"))
    healthy = _StreamResponseFake()
    healthy.push(_FRAME1)
    healthy.push(_FRAME2)
    healthy.end()
    a = _node("a", client=_StreamRespClient(dead))
    b = _node("b", client=_StreamRespClient(healthy))
    provider = _provider(_pool(a, b))

    chunks = []
    async for chunk in provider.stream(make_request(), make_resource()):
        chunks.append(chunk)
    assert chunks, "healthy node served the request"
    assert a.total_failures == 1
    assert a.current_in_flight == 0 and b.current_in_flight == 0


@pytest.mark.asyncio
async def test_429_on_node_a_next_attempt_uses_healthy_node_b():
    a = _node("a", client=_StreamRespClient(
        _StaticStreamResponse(
            429,
            b'{"error":{"code":429,"message":"rate","status":"RESOURCE_EXHAUSTED"}}',
        )
    ))
    b = _node("b", client=_StreamRespClient(_OkStream()))
    provider = _provider(_pool(a, b))

    resp = await provider.complete(make_request(), make_resource())
    assert _FRAME1_TEXT in resp.text
    assert a.total_rate_limits == 1 and a.cooldown_until > 0
    assert b.total_requests == 1
    # provider as a whole never died: the scheduler saw a success


class _OkStream:
    """Queue-driven 200 response with one full exchange."""

    status_code = 200
    headers = {}

    def __init__(self):
        self.closed = False
        self.aread_calls = 0
        self._data = [_FRAME1, _FRAME2, None]
        self._queue = asyncio.Queue()
        for item in self._data:
            self._queue.put_nowait(item)

    async def aiter_bytes(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def aread(self):
        self.aread_calls += 1
        return b""

    async def aclose(self):
        self.closed = True


# ---------------------------------------------------------------------------
# config wiring: node_pool section reaches the pool
# ---------------------------------------------------------------------------
def test_factory_parses_node_pool_config():
    from providers.anonymous_vertex.factory import AnonymousVertexProviderFactory

    provider = AnonymousVertexProviderFactory().create_provider(
        "anonymous_vertex",
        {
            "enabled": True,
            "node_pool": {
                "max_attempts": 2,
                "rate_limit_cooldown": {"base": 15.0, "factor": 3.0, "max": 120.0},
                "nodes": [
                    {"id": "node-a", "weight": 2, "max_concurrency": 4,
                     "proxy": {"scheme": "socks5", "host": "h", "port": 1080}},
                    {"id": "node-b", "enabled": False},
                ],
            },
        },
    )
    pool = provider.node_pool
    assert [n.node_id for n in pool.nodes] == ["node-a", "node-b"]
    na, nb = pool.nodes
    assert na.weight == 2 and na.max_concurrency == 4 and na.enabled
    assert na.spec.proxy == {"scheme": "socks5", "host": "h", "port": 1080}
    assert nb.enabled is False
    assert provider._max_node_attempts == 2

    # absent node_pool config -> single default node (back-compat)
    plain = AnonymousVertexProviderFactory().create_provider("anonymous_vertex", {})
    assert len(plain.node_pool.nodes) == 1
    assert plain.node_pool.nodes[0].node_id == "default"


# ---------------------------------------------------------------------------
# 12. streaming regression is covered by tests/providers/test_anonymous_vertex.py
#     (all ANON-002 streaming tests run against the node-aware provider).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ANON-004-FIX-01: NodeLease.release is idempotent
# ---------------------------------------------------------------------------
async def test_double_release_is_idempotent():
    node = _node("only")
    pool = _pool(node)
    lease = await pool.acquire()
    assert node.current_in_flight == 1
    await lease.release()
    assert node.current_in_flight == 0
    await lease.release()  # second release: no-op
    assert node.current_in_flight == 0
    await pool.release(lease)  # even a third path through pool.release
    assert node.current_in_flight == 0
    assert lease.released is True


async def test_release_via_cm_and_finally_overlap_counts_once():
    """A lease released both inside an ``async with`` body (as a finally
    would do) and again by the CM exit must decrement exactly once."""
    node = _node("only")
    pool = _pool(node)
    lease = await pool.acquire()
    async with lease:
        assert node.current_in_flight == 1
        await lease.release()  # e.g. provider's finally runs first
        assert node.current_in_flight == 0
    assert node.current_in_flight == 0  # CM exit is a no-op now

    # the node is fully available again
    lease2 = await pool.acquire()
    assert lease2 is not None
    await lease2.release()
    assert node.current_in_flight == 0


async def test_concurrent_double_release_counts_once():
    """Two racing release() calls decrement in_flight exactly once."""
    import asyncio as _asyncio

    node = _node("only")
    pool = _pool(node)
    lease = await pool.acquire()
    await _asyncio.gather(lease.release(), lease.release())
    assert node.current_in_flight == 0
