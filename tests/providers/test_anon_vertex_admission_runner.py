"""ANON-008-B acceptance tests: admission runner pipeline."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timezone

import pytest

from providers.anonymous_vertex.admission import (
    NodeAdmissionResult,
    NodeAdmissionState,
)
from providers.anonymous_vertex.admission_runner import (
    AdmissionAttempt,
    AdmissionRunner,
)
from providers.anonymous_vertex.node_definitions import NodeDefinition

NOW = datetime.now(timezone.utc)


def _definition(node_id="node-a"):
    return NodeDefinition(
        node_id=node_id,
        proxy={"scheme": "socks5", "host": "10.0.0.1", "port": 1080},
    )


def _ready():
    return NodeAdmissionResult(
        state=NodeAdmissionState.READY, reason=None, checked_at=NOW
    )


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------
async def test_success_returns_checker_result_and_records_attempt():
    checker_calls = []

    class OkChecker:
        async def check(self, node):
            checker_calls.append(node.node_id)
            return _ready()

    runner = AdmissionRunner(OkChecker())
    node = _definition()
    result = await runner.run(node)

    assert result is not None
    assert result.state == NodeAdmissionState.READY
    assert result.reason is None
    assert checker_calls == ["node-a"]  # called exactly once

    (attempt,) = runner.attempts
    assert isinstance(attempt, AdmissionAttempt)
    assert attempt.node_id == "node-a"
    assert attempt.result is result
    assert attempt.finished_at >= attempt.started_at
    assert attempt.finished_at.tzinfo is not None  # timezone-aware UTC
    assert attempt.started_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Checker exception -> FAILED / checker_exception (never escapes)
# ---------------------------------------------------------------------------
async def test_checker_exception_converted_to_failed():
    class BoomChecker:
        async def check(self, node):
            raise RuntimeError("connection failed")

    runner = AdmissionRunner(BoomChecker())
    result = await runner.run(_definition())

    assert result.state == NodeAdmissionState.FAILED
    assert result.reason == "checker_exception: connection failed"
    assert result.reason.startswith("checker_exception")
    assert result.checked_at.tzinfo is not None

    (attempt,) = runner.attempts
    assert attempt.result is result


# ---------------------------------------------------------------------------
# Timeout -> FAILED / timeout
# ---------------------------------------------------------------------------
async def test_timeout_converted_to_failed():
    class SlowChecker:
        async def check(self, node):
            await asyncio.sleep(5.0)
            return _ready()

    runner = AdmissionRunner(SlowChecker(), timeout_seconds=0.01)
    result = await asyncio.wait_for(runner.run(_definition()), timeout=2.0)

    assert result.state == NodeAdmissionState.FAILED
    assert result.reason == "timeout"


# ---------------------------------------------------------------------------
# Invalid checker result -> FAILED / invalid_checker_result
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_result", [None, {}, "ready", 42])
async def test_invalid_checker_result_converted_to_failed(bad_result):
    class BadChecker:
        async def check(self, node):
            return bad_result

    runner = AdmissionRunner(BadChecker())
    result = await runner.run(_definition())

    assert result.state == NodeAdmissionState.FAILED
    assert result.reason == "invalid_checker_result"


# ---------------------------------------------------------------------------
# Attempt lifecycle / immutability
# ---------------------------------------------------------------------------
async def test_attempt_is_immutable():
    class OkChecker:
        async def check(self, node):
            return _ready()

    runner = AdmissionRunner(OkChecker())
    await runner.run(_definition())
    attempt = runner.attempts[0]

    with pytest.raises(dataclasses.FrozenInstanceError):
        attempt.finished_at = NOW  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        attempt.node_id = "x"  # type: ignore[misc]


async def test_attempts_append_per_run():
    class OkChecker:
        async def check(self, node):
            return _ready()

    runner = AdmissionRunner(OkChecker())
    await runner.run(_definition("n1"))
    await runner.run(_definition("n2"))
    assert [a.node_id for a in runner.attempts] == ["n1", "n2"]


# ---------------------------------------------------------------------------
# Cancellation is never swallowed
# ---------------------------------------------------------------------------
async def test_cancellation_propagates_and_records_no_attempt():
    class ParkedChecker:
        async def check(self, node):
            await asyncio.sleep(30.0)
            return _ready()

    runner = AdmissionRunner(ParkedChecker(), timeout_seconds=10.0)
    task = asyncio.create_task(runner.run(_definition()))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.attempts == ()  # no half-recorded attempt


# ---------------------------------------------------------------------------
# Runner contract details
# ---------------------------------------------------------------------------
def test_runner_exposes_timeout_configuration():
    class OkChecker:
        async def check(self, node):
            return _ready()

    assert AdmissionRunner(OkChecker()).timeout_seconds == 30.0
    assert AdmissionRunner(OkChecker(), timeout_seconds=5).timeout_seconds == 5.0
