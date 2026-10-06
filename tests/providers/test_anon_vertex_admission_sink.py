"""ANON-011-C acceptance tests: admission result sink (fan-out bridge)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from providers.anonymous_vertex.admission import (
    NodeAdmissionResult,
    NodeAdmissionState,
)
from providers.anonymous_vertex.admission_orchestrator import (
    AdmissionPipelineResult,
)
from providers.anonymous_vertex.admission_sink import AdmissionResultSink
from providers.anonymous_vertex.admission_store import (
    InMemoryAdmissionResultStore,
)
from providers.anonymous_vertex.nodes import (
    AdmissionProjection,
    AnonymousVertexNodePool,
)

from tests.providers.test_anon_vertex_node_pool import FakeClock, _node

NOW = datetime.now(timezone.utc)


def _result(node_id, state):
    final = NodeAdmissionResult(
        state=state,
        reason=None if state == NodeAdmissionState.READY else "boom",
        checked_at=NOW,
    )
    return AdmissionPipelineResult(
        node_id=node_id, final_result=final, attempts=(final,)
    )


def _admission_pool(nodes):
    clock = FakeClock()
    pool = AnonymousVertexNodePool(
        list(nodes), now_fn=clock, admission_projection=AdmissionProjection()
    )
    pool.clock = clock
    return pool


class BrokenStore:
    def __init__(self):
        self.puts = 0

    async def put(self, result):
        self.puts += 1
        raise RuntimeError("store down")


# ---------------------------------------------------------------------------
# Happy path: one result fans out to BOTH consumers
# ---------------------------------------------------------------------------
async def test_result_reaches_store_and_pool():
    store = InMemoryAdmissionResultStore()
    pool = _admission_pool([_node("a")])
    sink = AdmissionResultSink(store, pool)

    await sink(_result("a", NodeAdmissionState.READY))

    assert await store.get("a") is not None          # store recorded
    lease = await pool.acquire()                      # pool admitted
    assert lease is not None and lease.node_id == "a"
    await lease.release()


async def test_non_ready_result_reaches_store_but_pool_stays_gated():
    store = InMemoryAdmissionResultStore()
    pool = _admission_pool([_node("a")])
    sink = AdmissionResultSink(store, pool)

    await sink(_result("a", NodeAdmissionState.QUARANTINED))

    assert (await store.get("a")).final_result.state == (
        NodeAdmissionState.QUARANTINED
    )
    assert await pool.acquire() is None               # gate stays closed


# ---------------------------------------------------------------------------
# Error semantics
# ---------------------------------------------------------------------------
async def test_store_failure_does_not_swallow_pool_update():
    store = BrokenStore()
    pool = _admission_pool([_node("a")])
    sink = AdmissionResultSink(store, pool)

    with pytest.raises(RuntimeError, match="store down"):
        await sink(_result("a", NodeAdmissionState.READY))

    assert store.puts == 1                            # store was attempted
    lease = await pool.acquire()                      # pool push still done
    assert lease is not None and lease.node_id == "a"
    await lease.release()


async def test_pool_push_failure_propagates():
    store = InMemoryAdmissionResultStore()
    pool = _admission_pool([_node("a")])
    sink = AdmissionResultSink(store, pool)

    with pytest.raises(KeyError):
        await sink(_result("ghost", NodeAdmissionState.READY))
    # store already recorded the fact before the pool rejected the push
    assert await store.get("ghost") is not None


async def test_cancellation_propagates():
    class ParkedStore:
        async def put(self, result):
            await asyncio.sleep(30.0)

    pool = _admission_pool([_node("a")])
    sink = AdmissionResultSink(ParkedStore(), pool)
    task = asyncio.create_task(sink(_result("a", NodeAdmissionState.READY)))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Scheduler integration: scheduler -> sink -> store + pool
# ---------------------------------------------------------------------------
async def test_scheduler_feeds_store_and_pool_through_sink():
    from providers.anonymous_vertex.admission import (
        NodeAdmissionResult as _R,
    )

    class FakeOrchestrator:
        async def check(self, node):
            final = _R(
                state=NodeAdmissionState.READY, reason=None, checked_at=NOW
            )
            return AdmissionPipelineResult(
                node_id=node.node_id, final_result=final, attempts=(final,)
            )

    nodes = [_node("a"), _node("b")]
    store = InMemoryAdmissionResultStore()
    pool = _admission_pool(nodes)
    sink = AdmissionResultSink(store, pool)
    scheduler = AdmissionSchedulerWithSink(FakeOrchestrator(), nodes, sink)

    await scheduler.start()
    deadline = asyncio.get_event_loop().time() + 2.0
    try:
        while len(await store.snapshot()) < 2:
            if asyncio.get_event_loop().time() > deadline:
                raise asyncio.TimeoutError("results did not arrive")
            await asyncio.sleep(0.01)
    finally:
        await scheduler.stop()

    # both consumers received everything
    assert {r.node_id for r in await store.snapshot()} == {"a", "b"}
    first = await pool.acquire()
    second = await pool.acquire()  # hold both: stable order cannot repeat
    assert first is not None and second is not None
    assert {first.node_id, second.node_id} == {"a", "b"}
    await pool.release(first)
    await pool.release(second)


class AdmissionSchedulerWithSink:
    """Thin wrapper mirroring the production wiring shape."""

    def __init__(self, orchestrator, nodes, sink):
        from providers.anonymous_vertex.admission_scheduler import (
            AdmissionScheduler,
        )

        self._scheduler = AdmissionScheduler(
            orchestrator, nodes, interval_seconds=60.0,
            max_concurrency=2, result_sink=sink,
        )

    async def start(self):
        await self._scheduler.start()

    async def stop(self):
        await self._scheduler.stop()
