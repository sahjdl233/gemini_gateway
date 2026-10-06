"""ANON-011-C acceptance tests: push + local projection in the node pool."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from providers.anonymous_vertex.admission import NodeAdmissionState
from providers.anonymous_vertex.admission_orchestrator import (
    AdmissionPipelineResult,
)
from providers.anonymous_vertex.admission_state import (
    AdmissionStateProvider,
    StoreAdmissionStateProvider,
)
from providers.anonymous_vertex.admission_store import (
    InMemoryAdmissionResultStore,
)
from providers.anonymous_vertex.admission import NodeAdmissionResult
from providers.anonymous_vertex.nodes import (
    AdmissionProjection,
    AnonymousVertexNodePool,
)

from tests.providers.test_anon_vertex_node_pool import FakeClock, _node

NOW = datetime.now(timezone.utc)


def _result(node_id, state):
    final = NodeAdmissionResult(state=state, reason=None, checked_at=NOW)
    return AdmissionPipelineResult(
        node_id=node_id, final_result=final, attempts=(final,)
    )


def _pool(nodes, *, projection=True, **kwargs):
    clock = FakeClock()
    pool = AnonymousVertexNodePool(
        list(nodes),
        now_fn=clock,
        admission_projection=AdmissionProjection() if projection else None,
        **kwargs,
    )
    pool.clock = clock
    return pool


async def _admit(pool, mapping):
    """Push a {node_id: state} mapping into the pool projection."""
    for node_id, state in mapping.items():
        await pool.update_admission_state(node_id, state)


# ---------------------------------------------------------------------------
# A. READY usable, non-READY never selected
# ---------------------------------------------------------------------------
async def test_only_ready_nodes_are_acquired():
    pool = _pool([_node("a"), _node("b"), _node("c")])
    await _admit(pool, {
        "a": NodeAdmissionState.READY,
        "b": NodeAdmissionState.FAILED,
        "c": NodeAdmissionState.QUARANTINED,
    })
    for _ in range(3):
        lease = await pool.acquire()
        assert lease is not None and lease.node_id == "a"
        await lease.release()


# ---------------------------------------------------------------------------
# B. every non-READY state is filtered
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("state", [
    NodeAdmissionState.UNKNOWN,
    NodeAdmissionState.TESTING,
    NodeAdmissionState.FAILED,
    NodeAdmissionState.QUARANTINED,
])
async def test_non_ready_states_are_not_candidates(state):
    pool = _pool([_node("only")])
    await pool.update_admission_state("only", state)
    assert await pool.acquire() is None


async def test_never_pushed_node_is_not_a_candidate():
    """admission-aware mode: no READY push yet -> not selectable."""
    pool = _pool([_node("a")])
    assert await pool.acquire() is None
    await pool.update_admission_state("a", NodeAdmissionState.UNKNOWN)
    assert await pool.acquire() is None
    await pool.update_admission_state("a", NodeAdmissionState.READY)
    lease = await pool.acquire()
    assert lease is not None and lease.node_id == "a"
    await lease.release()


# ---------------------------------------------------------------------------
# D. runtime eligibility still applies on top of READY
# ---------------------------------------------------------------------------
async def test_ready_node_still_subject_to_runtime_checks():
    node = _node("a", max_concurrency=1)
    pool = _pool([node])
    await pool.update_admission_state("a", NodeAdmissionState.READY)

    node.enabled = False
    assert await pool.acquire() is None
    node.enabled = True

    await pool.record_rate_limit("a")
    assert await pool.acquire() is None
    pool.clock.advance(31.0)

    await pool.record_recaptcha_failure("a")
    assert await pool.acquire() is None

    await pool.record_recaptcha_success("a")
    held = await pool.acquire()
    assert held is not None
    assert await pool.acquire() is None  # at max_concurrency
    await held.release()


# ---------------------------------------------------------------------------
# E. admission updates never mutate runtime state
# ---------------------------------------------------------------------------
async def test_admission_updates_do_not_touch_runtime_state():
    node = _node("a")
    pool = _pool([node])
    before = {
        "in_flight": node.current_in_flight,
        "cooldown": node.cooldown_until,
        "failures": node.total_failures,
        "rate_limits": node.total_rate_limits,
        "recaptcha": node.recaptcha_state,
        "enabled": node.enabled,
        "weight": node.weight,
    }
    for state in (
        NodeAdmissionState.READY,
        NodeAdmissionState.FAILED,
        NodeAdmissionState.QUARANTINED,
    ):
        await pool.update_admission_state("a", state)
    assert node.current_in_flight == before["in_flight"]
    assert node.cooldown_until == before["cooldown"]
    assert node.total_failures == before["failures"]
    assert node.total_rate_limits == before["rate_limits"]
    assert node.recaptcha_state == before["recaptcha"]
    assert node.enabled == before["enabled"]
    assert node.weight == before["weight"]
    assert node.definition is None  # NodeDefinition untouched


# ---------------------------------------------------------------------------
# F. legacy mode: no projection -> behaviour unchanged
# ---------------------------------------------------------------------------
async def test_legacy_pool_without_projection():
    pool = _pool([_node("a")], projection=False)
    lease = await pool.acquire()
    assert lease is not None and lease.node_id == "a"
    await lease.release()

    with pytest.raises(RuntimeError):
        await pool.update_admission_state("a", NodeAdmissionState.READY)


async def test_from_specs_accepts_projection():
    projection = AdmissionProjection()
    pool = AnonymousVertexNodePool.from_specs(
        [_node("a").spec], admission_projection=projection
    )
    assert pool._admission is projection


# ---------------------------------------------------------------------------
# update_admission_state error semantics
# ---------------------------------------------------------------------------
async def test_unknown_node_id_raises_key_error():
    pool = _pool([_node("a")])
    with pytest.raises(KeyError):
        await pool.update_admission_state("ghost", NodeAdmissionState.READY)
    assert pool._admission.snapshot() == {}  # no phantom entry


async def test_invalid_state_value_raises_value_error():
    pool = _pool([_node("a")])
    with pytest.raises(ValueError):
        await pool.update_admission_state("a", "ready")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# G. work-conserving: READY+capacity wins without waiting
# ---------------------------------------------------------------------------
async def test_work_conserving_skips_quarantined_and_full_nodes():
    """A=READY+full, B=QUARANTINED, C=READY+capacity: the very first
    candidate evaluation acquires C directly.  wait_for_capacity must not
    wait on quarantined nodes, nor on the full node A."""
    pool = _pool([
        _node("a", max_concurrency=1),
        _node("b"),
        _node("c"),
    ])
    await _admit(pool, {
        "a": NodeAdmissionState.READY,
        "b": NodeAdmissionState.QUARANTINED,
        "c": NodeAdmissionState.READY,
    })
    held = await pool.acquire()  # -> a (only admitted+capable at that point)
    assert held.node_id == "a"

    lease = await asyncio.wait_for(
        pool.acquire(wait_for_capacity=True), timeout=1.0
    )
    assert lease.node_id == "c"  # immediate: no waiting anywhere
    await lease.release()
    await held.release()


async def test_waiter_waits_for_admitted_capacity_not_quarantined():
    """Only A is READY (full), B is QUARANTINED: the waiter must NOT be
    handed B even though B has capacity."""
    pool = _pool([_node("a", max_concurrency=1), _node("b")])
    await _admit(pool, {
        "a": NodeAdmissionState.READY,
        "b": NodeAdmissionState.QUARANTINED,
    })
    held = await pool.acquire()  # -> a
    assert held.node_id == "a"

    waiter = asyncio.create_task(pool.acquire(wait_for_capacity=True))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not waiter.done(), "quarantined B must never satisfy the waiter"

    await held.release()  # A freed -> waiter takes A, not B
    lease = await asyncio.wait_for(waiter, timeout=1.0)
    assert lease.node_id == "a"
    await lease.release()


# ---------------------------------------------------------------------------
# Push notification: node becoming READY wakes capacity waiters
# ---------------------------------------------------------------------------
async def test_ready_push_wakes_capacity_waiter():
    pool = _pool([_node("a", max_concurrency=1), _node("b")])
    await pool.update_admission_state("a", NodeAdmissionState.READY)
    held = await pool.acquire()
    assert held.node_id == "a"

    waiter = asyncio.create_task(pool.acquire(wait_for_capacity=True))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not waiter.done()

    # the admission world says b is READY now -> notify -> waiter wakes
    await pool.update_admission_state("b", NodeAdmissionState.READY)
    lease = await asyncio.wait_for(waiter, timeout=1.0)
    assert lease.node_id == "b"
    await lease.release()
    await held.release()


async def test_quarantine_push_does_not_make_node_selectable():
    pool = _pool([_node("a")])
    await pool.update_admission_state("a", NodeAdmissionState.READY)
    await pool.update_admission_state("a", NodeAdmissionState.QUARANTINED)
    assert await pool.acquire() is None


# ---------------------------------------------------------------------------
# Projection observability
# ---------------------------------------------------------------------------
async def test_projection_snapshot_reflects_pushes():
    pool = _pool([_node("a"), _node("b")])
    await _admit(pool, {
        "a": NodeAdmissionState.READY,
        "b": NodeAdmissionState.QUARANTINED,
    })
    assert pool._admission.snapshot() == {
        "a": NodeAdmissionState.READY,
        "b": NodeAdmissionState.QUARANTINED,
    }


# ---------------------------------------------------------------------------
# Store adapter (bootstrap / read-model role only — never used by acquire)
# ---------------------------------------------------------------------------
async def test_store_adapter_projects_latest_state():
    store = InMemoryAdmissionResultStore()
    provider = StoreAdmissionStateProvider(store)
    assert isinstance(provider, AdmissionStateProvider)

    assert await provider.get_state("node-a") is None  # never checked
    await store.put(_result("node-a", NodeAdmissionState.FAILED))
    assert await provider.get_state("node-a") == NodeAdmissionState.FAILED
    await store.put(_result("node-a", NodeAdmissionState.READY))
    assert await provider.get_state("node-a") == NodeAdmissionState.READY
    assert await provider.get_state("ghost") is None
