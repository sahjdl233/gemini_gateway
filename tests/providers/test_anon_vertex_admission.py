"""ANON-008-A acceptance tests: node admission domain model."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone

import pytest

from providers.anonymous_vertex.admission import (
    ILLEGAL_TRANSITIONS,
    AdmissionChecker,
    NodeAdmissionRecord,
    NodeAdmissionResult,
    NodeAdmissionState,
    can_transition,
)
from providers.anonymous_vertex.node_definitions import NodeDefinition

NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def _definition(node_id="node-a"):
    return NodeDefinition(
        node_id=node_id,
        proxy={"scheme": "socks5", "host": "10.0.0.1", "port": 1080},
        source_id="sub-01",
    )


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------
def test_state_enum_members():
    assert {s.name for s in NodeAdmissionState} == {
        "UNKNOWN", "TESTING", "READY", "FAILED", "QUARANTINED",
    }
    assert [s.value for s in NodeAdmissionState] == [
        "unknown", "testing", "ready", "failed", "quarantined",
    ]
    # str enum: serializes as its plain string value
    assert NodeAdmissionState.READY == "ready"


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------
def test_result_is_immutable():
    result = NodeAdmissionResult(
        state=NodeAdmissionState.READY, reason=None, checked_at=NOW
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.state = NodeAdmissionState.FAILED  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.reason = "x"  # type: ignore[misc]


def test_record_is_immutable():
    result = NodeAdmissionResult(
        state=NodeAdmissionState.FAILED, reason="authentication_failed",
        checked_at=NOW,
    )
    record = NodeAdmissionRecord(node_id="node-a", result=result)
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.node_id = "xxx"  # type: ignore[misc]
    assert record.node_id == "node-a"
    assert record.state == NodeAdmissionState.FAILED
    assert record.checked_at == NOW


# ---------------------------------------------------------------------------
# Transition matrix (full 5x5)
# ---------------------------------------------------------------------------
_ALLOWED = {
    ("UNKNOWN", "TESTING"),
    ("TESTING", "READY"),
    ("TESTING", "FAILED"),
    ("FAILED", "TESTING"),
    ("FAILED", "QUARANTINED"),
    ("READY", "TESTING"),
    ("QUARANTINED", "TESTING"),
}


@pytest.mark.parametrize("old", list(NodeAdmissionState))
@pytest.mark.parametrize("new", list(NodeAdmissionState))
def test_full_transition_matrix(old, new):
    allowed = (old.name, new.name) in _ALLOWED
    assert can_transition(old, new) is allowed, f"{old} -> {new}"


def test_spec_mandated_illegal_jumps_are_explicit():
    for old, new in ILLEGAL_TRANSITIONS:
        assert not can_transition(old, new)
    assert (NodeAdmissionState.UNKNOWN, NodeAdmissionState.READY) in ILLEGAL_TRANSITIONS
    assert (NodeAdmissionState.UNKNOWN, NodeAdmissionState.QUARANTINED) in ILLEGAL_TRANSITIONS
    assert (NodeAdmissionState.QUARANTINED, NodeAdmissionState.READY) in ILLEGAL_TRANSITIONS


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def test_result_serialization_roundtrip_ready():
    result = NodeAdmissionResult(
        state=NodeAdmissionState.READY, reason=None, checked_at=NOW
    )
    raw = result.to_dict()
    assert raw == {
        "state": "ready",
        "reason": None,
        "checked_at": "2026-10-05T12:00:00+00:00",
    }
    # JSON-safe
    raw = json.loads(json.dumps(raw))
    restored = NodeAdmissionResult.from_dict(raw)
    assert restored == result


def test_result_serialization_roundtrip_failed():
    result = NodeAdmissionResult(
        state=NodeAdmissionState.FAILED, reason="authentication_failed",
        checked_at=NOW,
    )
    raw = json.loads(json.dumps(result.to_dict()))
    assert raw["state"] == "failed"
    assert raw["reason"] == "authentication_failed"
    assert NodeAdmissionResult.from_dict(raw) == result


def test_record_serialization_roundtrip():
    result = NodeAdmissionResult(
        state=NodeAdmissionState.QUARANTINED, reason="repeated_failures",
        checked_at=NOW,
    )
    record = NodeAdmissionRecord(node_id="node-a", result=result)
    raw = json.loads(json.dumps({
        "node_id": record.node_id,
        "result": record.result.to_dict(),
    }))
    restored = NodeAdmissionRecord(
        node_id=raw["node_id"],
        result=NodeAdmissionResult.from_dict(raw["result"]),
    )
    assert restored == record


# ---------------------------------------------------------------------------
# Protocol contract (fake checker, no HTTP)
# ---------------------------------------------------------------------------
class FakeChecker:
    """Minimal AdmissionChecker implementation for contract testing."""

    def __init__(self, state=NodeAdmissionState.READY, reason=None):
        self.state = state
        self.reason = reason
        self.checked: list = []

    async def check(self, node: NodeDefinition) -> NodeAdmissionResult:
        self.checked.append(node.node_id)
        return NodeAdmissionResult(
            state=self.state, reason=self.reason,
            checked_at=datetime.now(timezone.utc),
        )


async def test_fake_checker_satisfies_protocol_and_is_awaitable():
    checker: AdmissionChecker = FakeChecker()
    # structural conformance to the Protocol
    assert isinstance(checker, AdmissionChecker)

    node = _definition()
    result = await checker.check(node)
    assert isinstance(result, NodeAdmissionResult)
    assert result.state == NodeAdmissionState.READY
    assert result.reason is None
    assert checker.checked == ["node-a"]


async def test_fake_checker_failure_shape():
    checker: AdmissionChecker = FakeChecker(
        state=NodeAdmissionState.FAILED, reason="authentication_failed"
    )
    result = await checker.check(_definition("node-b"))
    assert result.state == NodeAdmissionState.FAILED
    assert result.reason == "authentication_failed"
