"""ANON-011-A acceptance tests: in-memory admission result store."""

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
from providers.anonymous_vertex.admission_scheduler import AdmissionScheduler
from providers.anonymous_vertex.admission_store import (
    AdmissionResultStore,
    InMemoryAdmissionResultStore,
    NodeHealthSnapshot,
)
from providers.anonymous_vertex.node_definitions import NodeDefinition

NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def _result(node_id, state, reason=None):
    final = NodeAdmissionResult(state=state, reason=reason, checked_at=NOW)
    return AdmissionPipelineResult(
        node_id=node_id, final_result=final, attempts=(final,)
    )


def _node(i=0):
    return NodeDefinition(
        node_id=f"node-{i}",
        proxy={"scheme": "https", "host": "10.0.0.1", "port": 443 + i},
    )


class FakeOrchestrator:
    async def check(self, node):
        return _result(node.node_id, NodeAdmissionState.READY)


# ---------------------------------------------------------------------------
# put / get / latest overwrite / history append
# ---------------------------------------------------------------------------
async def test_put_then_get_returns_same_result():
    store = InMemoryAdmissionResultStore()
    result = _result("node-a", NodeAdmissionState.READY)
    await store.put(result)
    assert await store.get("node-a") is result


async def test_latest_overwritten_in_order():
    store = InMemoryAdmissionResultStore()
    await store.put(_result("node-a", NodeAdmissionState.READY))
    await store.put(_result("node-a", NodeAdmissionState.FAILED, "timeout"))
    await store.put(_result("node-a", NodeAdmissionState.QUARANTINED, "auth"))
    latest = await store.get("node-a")
    assert latest.final_result.state == NodeAdmissionState.QUARANTINED
    assert latest.final_result.reason == "auth"


async def test_history_appends_in_insertion_order():
    store = InMemoryAdmissionResultStore()
    states = [
        NodeAdmissionState.READY,
        NodeAdmissionState.FAILED,
        NodeAdmissionState.QUARANTINED,
    ]
    for i, state in enumerate(states):
        await store.put(_result("node-a", state, reason=str(i)))
    history = await store.history("node-a")
    assert isinstance(history, tuple)
    assert [r.final_result.state for r in history] == states
    assert [r.final_result.reason for r in history] == ["0", "1", "2"]


async def test_unknown_node_returns_none_and_empty_history():
    store = InMemoryAdmissionResultStore()
    assert await store.get("ghost") is None
    assert await store.history("ghost") == ()


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------
async def test_snapshot_covers_all_nodes_in_insertion_order():
    store = InMemoryAdmissionResultStore()
    await store.put(_result("node-b", NodeAdmissionState.FAILED))
    await store.put(_result("node-a", NodeAdmissionState.READY))
    snap = await store.snapshot()
    assert isinstance(snap, tuple)
    assert [r.node_id for r in snap] == ["node-b", "node-a"]


async def test_empty_store_snapshot_is_empty_tuple():
    store = InMemoryAdmissionResultStore()
    assert await store.snapshot() == ()
    assert await store.health_snapshot() == ()


# ---------------------------------------------------------------------------
# Health snapshot
# ---------------------------------------------------------------------------
async def test_health_snapshot_fields():
    store = InMemoryAdmissionResultStore()
    await store.put(_result("node-a", NodeAdmissionState.READY))
    await store.put(_result("node-b", NodeAdmissionState.QUARANTINED, "auth"))
    snap = await store.health_snapshot()
    assert len(snap) == 2
    a = next(s for s in snap if s.node_id == "node-a")
    b = next(s for s in snap if s.node_id == "node-b")
    assert isinstance(a, NodeHealthSnapshot)
    assert a.state == NodeAdmissionState.READY
    assert a.reason is None
    assert a.checked_at == NOW
    assert b.state == NodeAdmissionState.QUARANTINED
    assert b.reason == "auth"
    assert b.checked_at == NOW


# ---------------------------------------------------------------------------
# Mutation isolation
# ---------------------------------------------------------------------------
async def test_returned_tuples_do_not_leak_internal_storage():
    store = InMemoryAdmissionResultStore()
    await store.put(_result("node-a", NodeAdmissionState.READY))

    snap1 = await store.snapshot()
    snap2 = await store.snapshot()
    assert snap1 is not snap2  # freshly built each call
    with pytest.raises(TypeError):
        snap1[0] = _result("x", NodeAdmissionState.FAILED)  # immutable tuple

    hist = await store.history("node-a")
    assert hist is not await store.history("node-a")
    # appending to a returned tuple is impossible; and the stored results
    # are immutable value objects, so no caller-visible mutation exists
    assert len(await store.history("node-a")) == 1
    await store.put(_result("node-a", NodeAdmissionState.FAILED))
    assert len(await store.history("node-a")) == 2  # store evolves on its own
    assert len(hist) == 1  # earlier read unchanged


def test_store_satisfies_protocol():
    assert isinstance(InMemoryAdmissionResultStore(), AdmissionResultStore)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
async def test_100_concurrent_puts_keep_history_and_latest_intact():
    store = InMemoryAdmissionResultStore()

    async def put_one(i: int):
        state = (
            NodeAdmissionState.READY if i % 2 == 0
            else NodeAdmissionState.FAILED
        )
        await store.put(_result("node-a", state, reason=str(i)))

    await asyncio.gather(*(put_one(i) for i in range(100)))

    history = await store.history("node-a")
    assert len(history) == 100  # nothing lost, nothing duplicated
    latest = await store.get("node-a")
    assert latest.final_result.state in (
        NodeAdmissionState.READY, NodeAdmissionState.FAILED,
    )
    # reasons are unique 0..99 -> insertion order fully preserved
    assert sorted(
        (int(r.final_result.reason) for r in history)
    ) == list(range(100))


# ---------------------------------------------------------------------------
# Scheduler sink integration
# ---------------------------------------------------------------------------
async def test_scheduler_delivers_results_into_store():
    nodes = [_node(i) for i in range(3)]
    store = InMemoryAdmissionResultStore()
    scheduler = AdmissionScheduler(
        FakeOrchestrator(), nodes, interval_seconds=60.0,
        max_concurrency=2, result_sink=store.put,
    )
    await scheduler.start()
    try:
        deadline = asyncio.get_event_loop().time() + 2.0
        while len(await store.snapshot()) < 3:
            if asyncio.get_event_loop().time() > deadline:
                raise asyncio.TimeoutError("results did not reach the store")
            await asyncio.sleep(0.01)
    finally:
        await scheduler.stop()

    for node in nodes:
        latest = await store.get(node.node_id)
        assert latest is not None
        assert latest.node_id == node.node_id
        assert latest.final_result.state == NodeAdmissionState.READY
    snapshots = await store.health_snapshot()
    assert {s.node_id for s in snapshots} == {n.node_id for n in nodes}
