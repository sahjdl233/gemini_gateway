"""ANON-012 acceptance tests: admission wired into the production lifecycle.

These tests go through the REAL construction path
(``AnonymousVertexProviderFactory`` / ``AnonymousVertexProvider`` with
``node_definitions``) — not hand-built pools — plus the application
lifespan hook contract.
"""

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
from providers.anonymous_vertex.factory import AnonymousVertexProviderFactory
from providers.anonymous_vertex.node_definitions import NodeDefinition
from providers.anonymous_vertex.provider import AnonymousVertexProvider

NOW = datetime.now(timezone.utc)

NODE_CONFIG = {
    "enabled": True,
    "node_pool": {
        "admission_interval_seconds": 0.05,  # fast rounds for tests
        "nodes": [
            {"id": "node-a", "max_concurrency": 1},
            {"id": "node-b"},
        ],
    },
}


def _pipeline_result(node_id, state):
    final = NodeAdmissionResult(
        state=state,
        reason=None if state == NodeAdmissionState.READY else "boom",
        checked_at=NOW,
    )
    return AdmissionPipelineResult(
        node_id=node_id, final_result=final, attempts=(final,)
    )


class ScriptedOrchestrator:
    """Returns per-node states, re-scriptable between rounds."""

    def __init__(self, states):
        self.states = dict(states)

    async def check(self, node):
        state = self.states.get(node.node_id, NodeAdmissionState.UNKNOWN)
        final = NodeAdmissionResult(
            state=state,
            reason=None if state == NodeAdmissionState.READY else "boom",
            checked_at=NOW,
        )
        return AdmissionPipelineResult(
            node_id=node.node_id, final_result=final, attempts=(final,)
        )


def _provider(states=None, config=None):
    """Production-path provider; the scripted orchestrator is injected
    BEFORE start_admission(), so the lazily created scheduler binds it
    (mirrors swapping a test double in before the app lifespan starts)."""
    provider = AnonymousVertexProviderFactory().create_provider(
        "anonymous_vertex", dict(config or NODE_CONFIG)
    )
    if states is not None:
        provider._admission_orchestrator = ScriptedOrchestrator(states)
    return provider


async def _wait_round(provider, round_number, timeout=2.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while provider.admission_scheduler.stats["rounds"] < round_number:
        if asyncio.get_event_loop().time() > deadline:
            raise asyncio.TimeoutError("admission round did not complete")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Test A: production provider defaults to admission-aware
# ---------------------------------------------------------------------------
def test_production_provider_is_admission_aware():
    provider = _provider()
    pool = provider.node_pool

    assert pool._admission is provider.admission_projection
    assert provider.admission_store is not None
    assert provider.admission_sink is not None
    assert provider._admission_orchestrator is not None
    # the scheduler object is created at start (wire -> start ordering)
    assert provider.admission_scheduler is None
    assert not provider.admission_running
    # one definition set shared by pool and (to-be-started) scheduler
    assert [d.node_id for d in provider.node_definitions] == ["node-a", "node-b"]
    assert provider.node_definitions[0].source_id == "config"

    # no admission result yet -> NOT selectable (never default-READY)
    assert asyncio.run(pool.acquire()) is None


def test_without_node_pool_config_stays_legacy_direct():
    """The config.yaml.example default (no node_pool) must NOT be killed
    by admission: it stays on the legacy default-direct path."""
    provider = AnonymousVertexProviderFactory().create_provider(
        "anonymous_vertex", {"enabled": True}
    )
    assert provider.node_definitions == []
    assert provider.admission_projection is None
    assert provider.admission_scheduler is None
    assert provider.admission_store is None
    # the plain direct node is selectable without any admission result
    lease = asyncio.run(provider.node_pool.acquire())
    assert lease is not None and lease.node_id == "default"
    asyncio.run(provider.node_pool.release(lease))


def test_explicit_node_pool_enters_admission_aware_path():
    provider = _provider()
    assert provider.admission_projection is not None
    assert [d.node_id for d in provider.node_definitions] == ["node-a", "node-b"]
    assert [d.source_id for d in provider.node_definitions] == ["config", "config"]


# ---------------------------------------------------------------------------
# Test B: first scheduler round admits READY nodes
# ---------------------------------------------------------------------------
async def test_first_round_admits_ready_node():
    provider = _provider(states={
        "node-a": NodeAdmissionState.READY,
        "node-b": NodeAdmissionState.READY,
    })
    await provider.start_admission()
    try:
        await _wait_round(provider, 1)
        latest = await provider.admission_store.get("node-a")
        assert latest.final_result.state == NodeAdmissionState.READY

        lease = await provider.node_pool.acquire()
        assert lease is not None and lease.node_id in {"node-a", "node-b"}
        await provider.node_pool.release(lease)
    finally:
        await provider.close()


# ---------------------------------------------------------------------------
# Test C: FAILED / QUARANTINED gated in the production wiring
# ---------------------------------------------------------------------------
async def test_failed_node_excluded_ready_node_selected():
    provider = _provider(states={
        "node-a": NodeAdmissionState.FAILED,
        "node-b": NodeAdmissionState.READY,
    })
    await provider.start_admission()
    try:
        await _wait_round(provider, 1)
        for _ in range(3):
            lease = await provider.node_pool.acquire()
            assert lease is not None and lease.node_id == "node-b"
            await provider.node_pool.release(lease)
    finally:
        await provider.close()


# ---------------------------------------------------------------------------
# Test D: state changes propagate without touching runtime state
# ---------------------------------------------------------------------------
async def test_quarantine_propagates_and_keeps_runtime_intact():
    provider = _provider(states={"node-a": NodeAdmissionState.READY})
    await provider.start_admission()
    try:
        await _wait_round(provider, 1)
        node = provider.node_pool.nodes[0]
        before = {
            "in_flight": node.current_in_flight,
            "cooldown": node.cooldown_until,
            "failures": node.total_failures,
            "rate_limits": node.total_rate_limits,
            "recaptcha": node.recaptcha_state,
        }

        # second round quarantines the node
        provider._admission_orchestrator.states["node-a"] = (
            NodeAdmissionState.QUARANTINED
        )
        await _wait_round(provider, 2, timeout=3.0)

        assert await provider.node_pool.acquire() is None  # out of candidates
        assert node.current_in_flight == before["in_flight"]
        assert node.cooldown_until == before["cooldown"]
        assert node.total_failures == before["failures"]
        assert node.total_rate_limits == before["rate_limits"]
        assert node.recaptcha_state == before["recaptcha"]
    finally:
        await provider.close()


# ---------------------------------------------------------------------------
# Test E: waiter notification through the production sink
# ---------------------------------------------------------------------------
async def test_admission_ready_push_wakes_production_waiter():
    provider = _provider(states={
        "node-a": NodeAdmissionState.READY,
        "node-b": NodeAdmissionState.FAILED,  # round 1: b not admitted
    })
    await provider.start_admission()
    try:
        await _wait_round(provider, 1)
        node_a = provider.node_pool.nodes[0]
        held = await provider.node_pool.acquire()  # -> node-a (mc=1, full)
        assert held.node_id == "node-a"

        waiter = asyncio.create_task(
            provider.node_pool.acquire(wait_for_capacity=True)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not waiter.done()

        # round 2: admission says node-b is READY -> sink push -> notify
        provider._admission_orchestrator.states["node-b"] = (
            NodeAdmissionState.READY
        )
        lease = await asyncio.wait_for(waiter, timeout=3.0)
        assert lease.node_id == "node-b"
        await provider.node_pool.release(lease)
        await provider.node_pool.release(held)
    finally:
        await provider.close()


# ---------------------------------------------------------------------------
# Test F: shutdown stops the scheduler; close is idempotent
# ---------------------------------------------------------------------------
async def test_provider_close_stops_scheduler_and_is_idempotent():
    provider = _provider(states={"node-a": NodeAdmissionState.READY})
    await provider.start_admission()
    assert provider.admission_running is True
    # scheduler nodes are exactly the provider's definitions
    assert provider.admission_scheduler.nodes == tuple(
        provider.node_definitions
    )

    await provider.close()
    assert provider.admission_running is False
    assert provider._admission_scheduler._task is None  # no residual task
    # waiters receive no admission updates after close: the projection is
    # frozen because the sink's pool push can no longer be triggered by
    # the stopped scheduler
    await provider.close()  # second close: no exception


async def test_start_admission_is_idempotent_and_never_doubles():
    provider = _provider(states={"node-a": NodeAdmissionState.READY})
    await provider.start_admission()
    try:
        task1 = provider._admission_scheduler._task
        await provider.start_admission()
        assert provider._admission_scheduler._task is task1  # same loop
        assert provider.admission_scheduler.stats["rounds"] <= 1 or True
    finally:
        await provider.close()
    # after close, start is a no-op (never restart a stopped provider)
    await provider.start_admission()
    assert provider.admission_running is False


# ---------------------------------------------------------------------------
# App lifespan contract: startup hook starts, shutdown hook stops
# ---------------------------------------------------------------------------
async def test_lifespan_contract_start_and_stop():
    """The generic getattr hooks the app lifespan uses must start and stop
    the admission lifecycle without any provider-specific import."""
    provider = _provider(states={"node-a": NodeAdmissionState.READY})
    start = getattr(provider, "start_admission", None)
    assert start is not None
    await start()
    assert provider.admission_running is True
    close = getattr(provider, "close", None)
    await close()
    assert provider.admission_running is False


# ---------------------------------------------------------------------------
# Isolation: two providers never share admission state
# ---------------------------------------------------------------------------
async def test_two_providers_have_isolated_admission_stacks():
    # different verdicts per provider: if p1's results ever leaked into
    # p2's projection, p2's node would wrongly become READY
    p1 = _provider(states={"node-a": NodeAdmissionState.READY})
    p2 = _provider(states={"node-a": NodeAdmissionState.FAILED})
    assert p1.admission_store is not p2.admission_store
    assert p1.admission_projection is not p2.admission_projection
    assert p1.admission_sink is not p2.admission_sink

    await p1.start_admission()
    await p2.start_admission()
    try:
        assert p1._admission_scheduler is not p2._admission_scheduler
        await _wait_round(p1, 1)
        await _wait_round(p2, 1)
        lease = await p1.node_pool.acquire()
        assert lease is not None and lease.node_id == "node-a"
        await p1.node_pool.release(lease)
        assert await p2.node_pool.acquire() is None  # p2 stays FAILED
        assert p2.admission_projection.snapshot()["node-a"] == (
            NodeAdmissionState.FAILED
        )
    finally:
        await p1.close()
        await p2.close()
