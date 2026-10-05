"""ANON-010-A acceptance tests: admission orchestrator fail-fast pipeline."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from providers.anonymous_vertex.admission import (
    NodeAdmissionResult,
    NodeAdmissionState,
)
from providers.anonymous_vertex.admission_orchestrator import (
    AdmissionOrchestrator,
)
from providers.anonymous_vertex.node_definitions import NodeDefinition

NOW = datetime.now(timezone.utc)


def _definition(node_id="node-a"):
    return NodeDefinition(
        node_id=node_id,
        proxy={"scheme": "https", "host": "1.2.3.4", "port": 443},
    )


def _result(state, name="r"):
    return NodeAdmissionResult(
        state=state,
        reason=None if state == NodeAdmissionState.READY else name,
        checked_at=NOW,
    )


class FakeChecker:
    """Records its own invocation order and returns a queued outcome."""

    def __init__(self, name, outcome):
        self.name = name
        self._outcome = outcome  # NodeAdmissionResult or Exception
        self.calls: list = []

    async def check(self, node):
        self.calls.append(node.node_id)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


# ---------------------------------------------------------------------------
# Execution order
# ---------------------------------------------------------------------------
async def test_checkers_run_in_injected_order():
    a = FakeChecker("a", _result(NodeAdmissionState.READY))
    b = FakeChecker("b", _result(NodeAdmissionState.READY))
    c = FakeChecker("c", _result(NodeAdmissionState.READY))
    orchestrator = AdmissionOrchestrator([a, b, c])

    pipeline = await orchestrator.check(_definition())

    assert [(ch.name, ch.calls) for ch in (a, b, c)] == [
        ("a", ["node-a"]),
        ("b", ["node-a"]),
        ("c", ["node-a"]),
    ]
    assert pipeline.attempts == (a._outcome, b._outcome, c._outcome)
    # checkers are kept in order and never recreated
    assert orchestrator.checkers == (a, b, c)


# ---------------------------------------------------------------------------
# All success
# ---------------------------------------------------------------------------
async def test_all_ready_yields_ready_with_full_attempts():
    a = FakeChecker("a", _result(NodeAdmissionState.READY))
    b = FakeChecker("b", _result(NodeAdmissionState.READY))
    c = FakeChecker("c", _result(NodeAdmissionState.READY))
    pipeline = await AdmissionOrchestrator([a, b, c]).check(_definition())

    assert pipeline.node_id == "node-a"
    assert pipeline.final_result.state == NodeAdmissionState.READY
    assert len(pipeline.attempts) == 3
    assert pipeline.final_result is pipeline.attempts[-1]


# ---------------------------------------------------------------------------
# Fail-fast
# ---------------------------------------------------------------------------
async def test_first_checker_failure_stops_pipeline():
    a = FakeChecker("a", _result(NodeAdmissionState.FAILED, "network_error"))
    b = FakeChecker("b", _result(NodeAdmissionState.READY))
    c = FakeChecker("c", _result(NodeAdmissionState.READY))
    pipeline = await AdmissionOrchestrator([a, b, c]).check(_definition())

    assert b.calls == [] and c.calls == []  # never executed
    assert pipeline.final_result is a._outcome  # original object preserved
    assert pipeline.final_result.reason == "network_error"
    assert pipeline.attempts == (a._outcome,)


async def test_second_checker_failure_stops_third():
    a = FakeChecker("a", _result(NodeAdmissionState.READY))
    b = FakeChecker("b", _result(NodeAdmissionState.FAILED, "captcha_required"))
    c = FakeChecker("c", _result(NodeAdmissionState.READY))
    pipeline = await AdmissionOrchestrator([a, b, c]).check(_definition())

    assert a.calls == ["node-a"]
    assert c.calls == []  # not executed after B failed
    assert len(pipeline.attempts) == 2
    assert pipeline.final_result is pipeline.attempts[-1]  # identity rule
    assert pipeline.final_result is b._outcome


# ---------------------------------------------------------------------------
# Empty checker list
# ---------------------------------------------------------------------------
def test_empty_checker_list_is_rejected():
    with pytest.raises(ValueError):
        AdmissionOrchestrator([])


# ---------------------------------------------------------------------------
# Exception / cancellation are never swallowed
# ---------------------------------------------------------------------------
async def test_checker_exception_propagates():
    a = FakeChecker("a", _result(NodeAdmissionState.READY))
    b = FakeChecker("b", RuntimeError("connection failed"))
    c = FakeChecker("c", _result(NodeAdmissionState.READY))

    with pytest.raises(RuntimeError, match="connection failed"):
        await AdmissionOrchestrator([a, b, c]).check(_definition())
    assert c.calls == []


async def test_cancellation_propagates():
    class CancellingChecker:
        async def check(self, node):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await AdmissionOrchestrator([CancellingChecker()]).check(_definition())


async def test_cancellation_while_second_checker_runs_propagates():
    a = FakeChecker("a", _result(NodeAdmissionState.READY))

    class ParkedChecker:
        async def check(self, node):
            await asyncio.sleep(30.0)

    task = asyncio.create_task(
        AdmissionOrchestrator([a, ParkedChecker()]).check(_definition())
    )
    await asyncio.sleep(0.02)
    assert a.calls == ["node-a"]  # first checker completed
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Composition shape: orchestrator -> classifier -> policy
# ---------------------------------------------------------------------------
async def test_pipeline_failure_flows_into_policy():
    from providers.anonymous_vertex.admission_policy import (
        AdmissionPolicy,
        DefaultFailureClassifier,
    )

    policy = AdmissionPolicy(classifier=DefaultFailureClassifier())
    a = FakeChecker("a", _result(NodeAdmissionState.FAILED, "captcha_required"))
    b = FakeChecker("b", _result(NodeAdmissionState.READY))

    pipeline = await AdmissionOrchestrator([a, b]).check(_definition())
    assert b.calls == []  # fail-fast
    outcome = policy.decide(pipeline.final_result)
    assert outcome.state == NodeAdmissionState.QUARANTINED
    assert outcome.reason == "captcha_required"  # reason untouched end-to-end
