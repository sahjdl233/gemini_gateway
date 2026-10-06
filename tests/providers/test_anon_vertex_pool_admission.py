"""ANON-011-B acceptance tests: admission-aware node pool selection."""

from __future__ import annotations

import asyncio

import pytest

from providers.anonymous_vertex.admission import (
    NodeAdmissionResult,
    NodeAdmissionState,
)
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
from providers.anonymous_vertex.nodes import AnonymousVertexNodePool

from tests.providers.test_anon_vertex_node_pool import FakeClock, _node

NOW_ACQUIRED = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)


class FakeAdmissionStates:
    """Configurable state provider (missing node -> None)."""

    def __init__(self, states=None):
        self.states = dict(states or {})

    async def get_state(self, node_id):
        return self.states.get(node_id)


def _pool(nodes, states=None, **kwargs):
    clock = FakeClock()
    provider = FakeAdmissionStates(states) if states is not None else None
    pool = AnonymousVertexNodePool(
        list(nodes), now_fn=clock, admission_state_provider=provider, **kwargs
    )
    pool.clock = clock
    return pool


def _result(node_id, state):
    final = NodeAdmissionResult(
        state=state, reason=None, checked_at=NOW_ACQUIRED
    )
    return AdmissionPipelineResult(
        node_id=node_id, final_result=final, attempts=(final,)
    )


# ---------------------------------------------------------------------------
# A. READY usable, non-READY never selected
# ---------------------------------------------------------------------------
async def test_only_ready_nodes_are_acquired():
    nodes = [_node("a"), _node("b"), _node("c")]
    pool = _pool(nodes, {
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
    pool = _pool([_node("only")], {"only": state})
    assert await pool.acquire() is None


# ---------------------------------------------------------------------------
# C. None means not admitted
# ---------------------------------------------------------------------------
async def test_missing_state_is_not_admitted():
    pool = _pool([_node("a")], {"a": None})
    assert await pool.acquire() is None

    # a node unknown to the provider (missing key -> None) likewise
    pool = _pool([_node("b")], {"other": NodeAdmissionState.READY})
    assert await pool.acquire() is None


async def test_provider_error_is_fail_closed():
    class BrokenProvider:
        async def get_state(self, node_id):
            raise RuntimeError("store down")

    pool = AnonymousVertexNodePool(
        [_node("a")], now_fn=FakeClock(), admission_state_provider=BrokenProvider()
    )
    assert await pool.acquire() is None  # never use an unverified node


# ---------------------------------------------------------------------------
# D. runtime eligibility still applies on top of READY
# ---------------------------------------------------------------------------
async def test_ready_node_still_subject_to_runtime_checks():
    node = _node("a", max_concurrency=1)
    pool = _pool([node], {"a": NodeAdmissionState.READY})

    # disabled
    node.enabled = False
    assert await pool.acquire() is None
    node.enabled = True

    # cooldown (429)
    await pool.record_rate_limit("a")
    assert await pool.acquire() is None
    pool.clock.advance(31.0)

    # recaptcha-blocked
    await pool.record_recaptcha_failure("a")
    assert await pool.acquire() is None

    # max_concurrency full (recaptcha recovered via the pool's own API,
    # which clears the recaptcha cooldown too)
    await pool.record_recaptcha_success("a")
    held = await pool.acquire()
    assert held is not None  # healthy + admitted again
    assert await pool.acquire() is None  # but at max_concurrency
    await held.release()


# ---------------------------------------------------------------------------
# E. admission filtering never mutates runtime state
# ---------------------------------------------------------------------------
async def test_admission_filtering_does_not_touch_runtime_state():
    node = _node("a", max_concurrency=2)
    pool = _pool([node], {"a": NodeAdmissionState.READY})

    node.current_in_flight = 1
    node.cooldown_until = pool.clock.t + 500.0  # future cooldown
    node.total_failures = 5
    node.recaptcha_state = "failed"  # runtime blocks it, not admission
    before = {
        "in_flight": node.current_in_flight,
        "cooldown": node.cooldown_until,
        "failures": node.total_failures,
        "recaptcha": node.recaptcha_state,
    }

    for _ in range(3):
        await pool.acquire()  # filtered out: recaptcha-failed at runtime
    assert node.current_in_flight == before["in_flight"]
    assert node.cooldown_until == before["cooldown"]
    assert node.total_failures == before["failures"]
    assert node.recaptcha_state == before["recaptcha"]

    # a successful acquire/release still does its normal accounting
    node.recaptcha_state = "ok"
    node.cooldown_until = 0.0
    lease = await pool.acquire()
    assert lease is not None
    assert node.current_in_flight == 2  # 1 (pre-set) + 1 (acquired)
    await lease.release()
    assert node.current_in_flight == 1


# ---------------------------------------------------------------------------
# F. backward compatibility: no provider -> old behaviour
# ---------------------------------------------------------------------------
async def test_without_provider_behaviour_unchanged():
    node = _node("a")
    pool = AnonymousVertexNodePool([node], now_fn=FakeClock())
    assert isinstance(pool, AnonymousVertexNodePool)
    # no admission data exists at all — the node is still selectable
    lease = await pool.acquire()
    assert lease is not None and lease.node_id == "a"
    await lease.release()


# ---------------------------------------------------------------------------
# G. work-conserving regression under admission filtering
# ---------------------------------------------------------------------------
async def test_work_conserving_skips_quarantined_and_full_nodes():
    a = _node("a", max_concurrency=1)  # READY but full
    b = _node("b")                     # QUARANTINED and idle
    c = _node("c")                     # READY with capacity
    pool = _pool([a, b, c], {
        "a": NodeAdmissionState.READY,
        "b": NodeAdmissionState.QUARANTINED,
        "c": NodeAdmissionState.READY,
    })
    held = await pool.acquire()  # -> a (only admitted+capable node)
    assert held.node_id == "a"

    waiter = asyncio.create_task(pool.acquire(wait_for_capacity=True))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not waiter.done()
    # waiter must NOT wake on b's admission change (there is none) nor on
    # a's release — it must take c directly via its own eligibility
    # (c is acquired immediately in the same first pass, actually:
    # assert the waiter is done with c)
    lease = await asyncio.wait_for(waiter, timeout=1.0)
    assert lease.node_id == "c"
    await lease.release()
    await held.release()


# ---------------------------------------------------------------------------
# Store adapter
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


async def test_store_adapter_gates_pool_selection():
    store = InMemoryAdmissionResultStore()
    node = _node("a")
    pool = AnonymousVertexNodePool(
        [node], now_fn=FakeClock(), admission_state_provider=StoreAdmissionStateProvider(store)
    )
    # nothing in the store -> not admitted
    assert await pool.acquire() is None

    # READY result lands in the store -> node becomes selectable
    await store.put(_result("a", NodeAdmissionState.READY))
    lease = await pool.acquire()
    assert lease is not None and lease.node_id == "a"
    await lease.release()
