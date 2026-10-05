"""ANON-010-B acceptance tests: admission periodic scheduler."""

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
from providers.anonymous_vertex.node_definitions import NodeDefinition

NOW = datetime.now(timezone.utc)


def _node(i=0):
    return NodeDefinition(
        node_id=f"node-{i}",
        proxy={"scheme": "https", "host": "10.0.0.1", "port": 443 + i},
    )


def _pipeline_result(node: NodeDefinition):
    return AdmissionPipelineResult(
        node_id=node.node_id,
        final_result=NodeAdmissionResult(
            state=NodeAdmissionState.READY, reason=None, checked_at=NOW
        ),
        attempts=(),
    )


class FakeOrchestrator:
    """Async check(node) with optional delay and concurrency tracking."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls: list = []
        self.current = 0
        self.max_seen = 0

    async def check(self, node):
        self.calls.append(node.node_id)
        self.current += 1
        self.max_seen = max(self.max_seen, self.current)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.current -= 1
        return _pipeline_result(node)


class Sink:
    def __init__(self, fail_for=()):
        self.fail_for = frozenset(fail_for)
        self.received: list = []

    async def __call__(self, result):
        if result.node_id in self.fail_for:
            raise RuntimeError(f"sink failure for {result.node_id}")
        self.received.append(result.node_id)


async def _wait_until(predicate, timeout=2.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise asyncio.TimeoutError("condition not met in time")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Immediate first run + per-node sink delivery
# ---------------------------------------------------------------------------
async def test_first_run_is_immediate_and_every_node_reaches_sink():
    nodes = [_node(i) for i in range(4)]
    sink = Sink()
    scheduler = AdmissionScheduler(
        FakeOrchestrator(), nodes, interval_seconds=60.0,
        max_concurrency=3, result_sink=sink,
    )
    await scheduler.start()
    try:
        # interval is 60s: results must arrive LONG before it elapses
        await _wait_until(lambda: len(sink.received) == 4)
        assert set(sink.received) == {n.node_id for n in nodes}
        assert scheduler.stats["rounds"] == 1
        assert scheduler.stats["results"] == 4
        assert scheduler.stats["sink_errors"] == 0
    finally:
        await scheduler.stop()
    assert not scheduler.running


async def test_periodic_execution_repeats():
    nodes = [_node(0), _node(1)]
    sink = Sink()
    scheduler = AdmissionScheduler(
        FakeOrchestrator(), nodes, interval_seconds=0.01,
        max_concurrency=2, result_sink=sink,
    )
    await scheduler.start()
    try:
        await _wait_until(lambda: scheduler.stats["rounds"] >= 3)
        # every round delivers every node
        assert len(sink.received) >= 6
    finally:
        await scheduler.stop()


# ---------------------------------------------------------------------------
# Concurrency limit
# ---------------------------------------------------------------------------
async def test_concurrency_limit_is_respected():
    nodes = [_node(i) for i in range(20)]
    orchestrator = FakeOrchestrator(delay=0.02)
    sink = Sink()
    scheduler = AdmissionScheduler(
        orchestrator, nodes, interval_seconds=60.0,
        max_concurrency=3, result_sink=sink,
    )
    await scheduler.start()
    try:
        await _wait_until(lambda: len(sink.received) == 20)
    finally:
        await scheduler.stop()
    assert orchestrator.max_seen <= 3
    assert orchestrator.max_seen > 1  # concurrency actually happened


# ---------------------------------------------------------------------------
# Sink failure isolation
# ---------------------------------------------------------------------------
async def test_sink_failure_isolated_per_node():
    nodes = [_node(i) for i in range(5)]
    sink = Sink(fail_for={"node-2"})
    scheduler = AdmissionScheduler(
        FakeOrchestrator(), nodes, interval_seconds=60.0,
        max_concurrency=5, result_sink=sink,
    )
    await scheduler.start()
    try:
        await _wait_until(lambda: scheduler.stats["rounds"] >= 1
                          and scheduler.stats["results"]
                          + scheduler.stats["sink_errors"] >= 5)
    finally:
        await scheduler.stop()
    # node-2 failed; the other four were delivered and the loop survived
    assert scheduler.stats["sink_errors"] == 1
    assert set(sink.received) == {n.node_id for n in nodes} - {"node-2"}
    assert scheduler.stats["results"] == 4


# ---------------------------------------------------------------------------
# Lifecycle: re-entrancy, idempotent stop, cancellation
# ---------------------------------------------------------------------------
async def test_start_twice_raises_runtime_error():
    scheduler = AdmissionScheduler(
        FakeOrchestrator(), [_node()], interval_seconds=60.0,
        max_concurrency=1, result_sink=Sink(),
    )
    await scheduler.start()
    try:
        with pytest.raises(RuntimeError):
            await scheduler.start()
    finally:
        await scheduler.stop()


async def test_stop_is_idempotent_and_safe_before_start():
    scheduler = AdmissionScheduler(
        FakeOrchestrator(), [_node()], interval_seconds=60.0,
        max_concurrency=1, result_sink=Sink(),
    )
    await scheduler.stop()  # before any start: no error
    await scheduler.start()
    await scheduler.stop()
    await scheduler.stop()  # twice: no error
    assert not scheduler.running


async def test_stop_cancels_in_flight_round_and_leaves_no_task():
    class SlowOrchestrator:
        async def check(self, node):
            await asyncio.sleep(30.0)

    scheduler = AdmissionScheduler(
        SlowOrchestrator(), [_node()], interval_seconds=60.0,
        max_concurrency=1, result_sink=Sink(),
    )
    await scheduler.start()
    await asyncio.sleep(0.02)  # round parked inside orchestrator.check
    await asyncio.wait_for(scheduler.stop(), timeout=2.0)
    assert not scheduler.running
    assert scheduler._task is None  # no residual background task


async def test_empty_nodes_runs_cleanly():
    sink = Sink()
    scheduler = AdmissionScheduler(
        FakeOrchestrator(), [], interval_seconds=0.01,
        max_concurrency=2, result_sink=sink,
    )
    await scheduler.start()
    try:
        await _wait_until(lambda: scheduler.stats["rounds"] >= 2)
        assert sink.received == []
        assert scheduler.stats["results"] == 0
    finally:
        await scheduler.stop()
