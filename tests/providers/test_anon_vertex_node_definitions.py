"""ANON-006 acceptance tests: node definition / runtime-state boundary."""

from __future__ import annotations

import json

import pytest

from providers.anonymous_vertex.node_definitions import (
    NodeDefinition,
    NodeRuntimeState,
    NodeSource,
    SOURCE_TYPE_MANUAL,
    SOURCE_TYPE_SUBSCRIPTION,
    SOURCE_TYPE_TXT,
    NodeSourceTypeError,
)
from providers.anonymous_vertex.nodes import AnonymousVertexNodePool, ExecutionNode

from tests.providers.test_anon_vertex_node_pool import FakeClock, _node

#: Fields owned by the runtime; a definition serialization must never
#: contain any of these.
_RUNTIME_FIELDS = {
    "current_in_flight",
    "cooldown_until",
    "consecutive_rate_limits",
    "consecutive_failures",
    "total_requests",
    "total_failures",
    "total_rate_limits",
    "recaptcha_state",
    "recaptcha_passes",
    "recaptcha_failures",
    "last_latency_s",
    "last_error",
}

#: Fields owned by the definition; a runtime-state serialization must never
#: contain any of these (node_id is the join key, present on both).
_DEFINITION_FIELDS = {
    "proxy",
    "source_id",
    "enabled",
    "weight",
    "max_concurrency",
    "metadata",
}


def _definition(node_id="node-a", **overrides):
    payload = {
        "node_id": node_id,
        "proxy": {"scheme": "socks5", "host": "10.0.0.1", "port": 1080},
        "source_id": "sub-01",
        "enabled": True,
        "weight": 2,
        "max_concurrency": 4,
        "metadata": {"region": "hk", "imported_at": "2026-10-05"},
    }
    payload.update(overrides)
    return NodeDefinition(**payload)


# ---------------------------------------------------------------------------
# Definition / Runtime separation
# ---------------------------------------------------------------------------
def test_runtime_mutation_does_not_change_definition():
    definition = _definition()
    node = ExecutionNode.from_definition(definition)
    assert node.definition is definition

    node.current_in_flight += 3
    node.cooldown_until = 999.0
    node.consecutive_rate_limits = 2
    node.total_failures = 7
    node.recaptcha_state = "failed"
    node.last_latency_s = 1.23
    node.last_error = "boom"

    # definition is frozen AND unchanged in value
    with pytest.raises(Exception):
        definition.weight = 99  # type: ignore[misc]
    assert definition.proxy == {"scheme": "socks5", "host": "10.0.0.1", "port": 1080}
    assert definition.weight == 2
    assert definition.max_concurrency == 4
    assert definition.enabled is True
    assert definition.metadata == {"region": "hk", "imported_at": "2026-10-05"}
    assert node.weight == 2  # node works on its own copy


def test_runtime_state_is_independent_object():
    node = ExecutionNode.from_definition(_definition())
    node.total_requests = 5
    state = node.capture_runtime_state()
    node.total_requests = 99  # later mutation does not touch the snapshot
    assert state.total_requests == 5
    assert NodeRuntimeState.capture(node).total_requests == 99


# ---------------------------------------------------------------------------
# Serialization boundary
# ---------------------------------------------------------------------------
def test_definition_serialization_has_no_runtime_fields():
    raw = _definition().to_dict()
    assert not _RUNTIME_FIELDS & set(raw), _RUNTIME_FIELDS & set(raw)
    # JSON-safe roundtrip
    assert json.loads(json.dumps(raw)) == raw
    rebuilt = NodeDefinition.from_dict(raw)
    assert rebuilt == _definition()


def test_runtime_serialization_has_no_definition_fields():
    state = NodeRuntimeState(node_id="node-a", total_requests=3, last_error="x")
    raw = state.to_dict()
    assert not _DEFINITION_FIELDS & set(raw), _DEFINITION_FIELDS & set(raw)
    assert json.loads(json.dumps(raw)) == raw
    rebuilt = NodeRuntimeState.from_dict(raw)
    assert rebuilt == state
    # unknown keys (e.g. from a future schema) are ignored, not fatal
    partial = NodeRuntimeState.from_dict({"node_id": "n", "proxy": {"a": 1}})
    assert partial.node_id == "n"


# ---------------------------------------------------------------------------
# Source attribution
# ---------------------------------------------------------------------------
def test_source_attribution_lives_on_definition_only():
    source = NodeSource(
        source_id="sub-01", source_type=SOURCE_TYPE_SUBSCRIPTION, name="机场 A"
    )
    definition = _definition(source_id="sub-01")
    node = ExecutionNode.from_definition(definition)
    state = node.capture_runtime_state()

    assert definition.source_id == "sub-01"
    assert source.source_type == SOURCE_TYPE_SUBSCRIPTION
    assert source.to_dict()["source_id"] == "sub-01"
    # runtime state carries no source payload
    assert "source_id" not in state.to_dict()
    assert not hasattr(state, "source_type")

    # source roundtrip
    assert NodeSource.from_dict(source.to_dict()) == source


def test_source_type_validation():
    for source_type in (SOURCE_TYPE_SUBSCRIPTION, SOURCE_TYPE_TXT, SOURCE_TYPE_MANUAL):
        NodeSource(source_id="s", source_type=source_type)
    with pytest.raises(NodeSourceTypeError):
        NodeSource(source_id="s", source_type="telegram")


# ---------------------------------------------------------------------------
# Reconstruction
# ---------------------------------------------------------------------------
def test_reconstruction_same_definition_fresh_runtime():
    definition = _definition()
    node1 = ExecutionNode.from_definition(definition)
    node1.total_failures = 4
    node1.recaptcha_state = "failed"
    node1.cooldown_until = 123.0

    node2 = ExecutionNode.from_definition(definition)
    assert node2.definition is definition
    assert node2.total_failures == 0
    assert node2.recaptcha_state == "unknown"
    assert node2.cooldown_until == 0.0
    assert node2.weight == node1.weight == definition.weight


def test_runtime_state_inheritance_is_caller_decision():
    definition = _definition()
    old = ExecutionNode.from_definition(definition)
    old.total_requests = 10
    old.total_rate_limits = 2
    old.last_latency_s = 0.5
    captured = old.capture_runtime_state()

    fresh = ExecutionNode.from_definition(definition)  # no state -> fresh
    assert fresh.total_requests == 0

    inherited = ExecutionNode.from_definition(  # explicit inheritance
        definition, runtime_state=captured
    )
    assert inherited.total_requests == 10
    assert inherited.total_rate_limits == 2
    assert inherited.last_latency_s == 0.5
    # definition untouched by inheritance
    assert inherited.definition == fresh.definition == definition


# ---------------------------------------------------------------------------
# Execution integration: a definition-built node behaves like any node
# ---------------------------------------------------------------------------
async def test_definition_node_in_pool_lifecycle():
    definition = _definition("node-a")
    node = ExecutionNode.from_definition(definition)
    clock = FakeClock()
    pool = AnonymousVertexNodePool([node], now_fn=clock)

    lease = await pool.acquire()
    assert lease.node is node
    assert node.current_in_flight == 1
    await pool.record_rate_limit("node-a")
    await lease.release()  # idempotent release semantics untouched

    await pool.record_rate_limit("node-a")
    assert node.cooldown_until > 0
    assert (await pool.acquire()) is None  # cooldown semantics unchanged

    clock.advance(61.0)  # second 429 escalated the ladder to 60s
    lease2 = await pool.acquire()
    assert lease2 is not None
    await lease2.release()

    # runtime counters survive as a snapshot; definition stays pristine
    snapshot = node.capture_runtime_state()
    assert snapshot.total_rate_limits == 2
    assert definition.to_dict() == _definition("node-a").to_dict()
