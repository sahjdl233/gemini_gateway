"""TASK-STATE-001: InMemoryPool.reconcile_resources() runtime-state contract.

Runtime state (health, cooldown_until, consecutive_failures,
total_requests, total_failures) survives a Resource being re-created from a
new definition, keyed by ResourceKey.  Everything else -- identity,
configuration, provider-specific fields and credential material -- always
comes from the *new* object.  in_flight is never inherited and blocks
reconcile entirely while non-zero.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from core.cooldown import CooldownManager
from core.health import HealthState
from core.pool import InMemoryPool
from core.resource import Resource
from providers.gemini_cli.resource import GeminiCliResource
from tests.conftest import make_cooldown, make_pool, make_resources


def _cooldown_until(clock, seconds: float = 30.0):
    return clock.now + timedelta(seconds=seconds)


def _make(resource_id: str, **overrides) -> Resource:
    spec = {"id": resource_id}
    spec.update(overrides)
    return make_resources([spec])[0]


# 1) Same ResourceKey keeps its runtime state.
async def test_reconcile_preserves_runtime_state_for_same_key(fake_clock):
    pool = make_pool(fake_clock, make_resources([{"id": "r1"}, {"id": "r2"}]))
    old_r1 = pool.resources[0]
    deadline = _cooldown_until(fake_clock)
    old_r1.health = HealthState.DEGRADED
    old_r1.cooldown_until = deadline
    old_r1.consecutive_failures = 4
    old_r1.total_requests = 17
    old_r1.total_failures = 5

    await pool.reconcile_resources([_make("r1"), _make("r2")])

    merged = pool.resources[0]
    assert merged is not pool.resources[1]
    assert merged.id == "r1"
    assert merged.health is HealthState.DEGRADED
    assert merged.cooldown_until == deadline
    assert merged.consecutive_failures == 4
    assert merged.total_requests == 17
    assert merged.total_failures == 5


# 2) Configuration on the new object always wins over the old object.
async def test_reconcile_new_configuration_wins(fake_clock):
    pool = make_pool(fake_clock, make_resources([
        {"id": "r1", "reply_text": "old"},
    ]))
    old_r1 = pool.resources[0]
    old_r1.total_failures = 9
    old_r1.scenario = "server_error"

    new_r1 = _make("r1", scenario="success", reply_text="new")
    new_r1.enabled = False
    new_r1.credential_id = "cred-new"
    await pool.reconcile_resources([new_r1])

    merged = pool.resources[0]
    assert merged.scenario == "success"
    assert merged.reply_text == "new"
    assert merged.enabled is False
    assert merged.credential_id == "cred-new"
    # Runtime state is still inherited.
    assert merged.total_failures == 9


# 3) Replacing a resource that still has in-flight requests must fail.
async def test_reconcile_replacement_with_in_flight_fails(fake_clock):
    pool = make_pool(fake_clock, make_resources([{"id": "r1"}]))
    old_r1 = pool.resources[0]
    old_r1.in_flight = 1

    with pytest.raises(RuntimeError, match="in-flight"):
        await pool.reconcile_resources([_make("r1")])

    assert pool.resources[0] is old_r1


# 4) Deleting a resource that still has in-flight requests must fail.
async def test_reconcile_deletion_with_in_flight_fails(fake_clock):
    pool = make_pool(fake_clock, make_resources([{"id": "r1"}, {"id": "r2"}]))
    old_r1 = pool.resources[0]
    old_r2 = pool.resources[1]
    old_r2.in_flight = 2

    with pytest.raises(RuntimeError, match="being removed"):
        await pool.reconcile_resources([_make("r1")])

    assert [r.id for r in pool.resources] == ["r1", "r2"]
    assert pool.resources[0] is old_r1
    assert pool.resources[1] is old_r2


# 5) An idle resource may be deleted.
async def test_reconcile_deletes_idle_resource(fake_clock):
    pool = make_pool(fake_clock, make_resources([
        {"id": "r1"},
        {"id": "r2"},
        {"id": "r3"},
    ]))
    pool.resources[0].total_requests = 3
    pool.resources[2].total_requests = 9

    await pool.reconcile_resources([_make("r1"), _make("r3")])

    assert [r.id for r in pool.resources] == ["r1", "r3"]
    assert pool.resources[0].total_requests == 3
    assert pool.resources[1].total_requests == 9


# 6) A brand new resource may join the pool.
async def test_reconcile_adds_new_resource(fake_clock):
    pool = make_pool(fake_clock, make_resources([{"id": "r1"}]))
    old_r1 = pool.resources[0]
    old_r1.total_requests = 8

    await pool.reconcile_resources([_make("r1"), _make("r2")])

    assert [r.id for r in pool.resources] == ["r1", "r2"]
    assert pool.resources[0].total_requests == 8
    assert pool.resources[1].total_requests == 0
    assert pool.resources[1].health is HealthState.HEALTHY


# 7) Reordering the definition list must not shuffle state between keys.
async def test_reconcile_reorder_does_not_cross_states(fake_clock):
    pool = make_pool(fake_clock, make_resources([
        {"id": "r1"},
        {"id": "r2"},
        {"id": "r3"},
    ]))
    for index, total in ((0, 11), (1, 22), (2, 33)):
        pool.resources[index].total_requests = total
        pool.resources[index].consecutive_failures = index + 1

    await pool.reconcile_resources([_make("r3"), _make("r1"), _make("r2")])

    by_id = {r.id: r for r in pool.resources}
    assert [r.id for r in pool.resources] == ["r3", "r1", "r2"]
    assert by_id["r1"].total_requests == 11
    assert by_id["r1"].consecutive_failures == 1
    assert by_id["r2"].total_requests == 22
    assert by_id["r2"].consecutive_failures == 2
    assert by_id["r3"].total_requests == 33
    assert by_id["r3"].consecutive_failures == 3


# 8) When the cursor target disappears, the cursor moves forward to the
#    first surviving key; otherwise it stays on the same key.
async def test_reconcile_cursor_moves_when_target_removed(fake_clock):
    pool = make_pool(fake_clock, make_resources([
        {"id": "r1"},
        {"id": "r2"},
        {"id": "r3"},
    ]))
    pool._cursor = 1  # points at r2

    await pool.reconcile_resources([_make("r1"), _make("r3")])

    assert pool.resources[pool._cursor].id == "r3"


async def test_reconcile_cursor_stays_on_same_key(fake_clock):
    pool = make_pool(fake_clock, make_resources([
        {"id": "r1"},
        {"id": "r2"},
        {"id": "r3"},
    ]))
    pool._cursor = 1  # points at r2

    await pool.reconcile_resources([_make("r1"), _make("r2")])

    assert pool.resources[pool._cursor].id == "r2"


# 9) Same local id under a different provider is a different ResourceKey.
async def test_reconcile_resource_key_isolates_same_id_across_providers(fake_clock):
    cooldown: CooldownManager = make_cooldown(fake_clock)
    shared_id = "acc-01"
    old_a = GeminiCliResource(id=shared_id, provider="gemini_cli", tier="FREE")
    old_b = GeminiCliResource(id=shared_id, provider="antigravity", tier="FREE")
    old_a.total_failures = 7
    old_b.total_failures = 3
    pool = InMemoryPool(
        provider="gemini_cli",
        resources=[old_a, old_b],
        cooldown=cooldown,
    )

    new_a = GeminiCliResource(
        id=shared_id, provider="gemini_cli", tier="PRO", project_id="proj-new"
    )
    await pool.reconcile_resources([new_a, old_b])

    assert old_a.resource_key != old_b.resource_key
    assert pool.resources[0].provider == "gemini_cli"
    assert pool.resources[0].total_failures == 7
    assert pool.resources[1].provider == "antigravity"
    assert pool.resources[1].total_failures == 3
    assert pool.resources[0].tier == "PRO"


# 10) A rejected reconcile leaves the pool completely untouched.
async def test_reconcile_failure_is_atomic(fake_clock):
    pool = make_pool(fake_clock, make_resources([
        {"id": "r1"},
        {"id": "r2"},
        {"id": "r3"},
    ]))
    old = list(pool.resources)
    old[0].total_requests = 5
    old[1].total_requests = 6
    old[2].total_requests = 7
    old[2].in_flight = 1
    pool._cursor = 2

    with pytest.raises(RuntimeError):
        await pool.reconcile_resources([
            _make("r1"),
            _make("r2"),
            _make("r3"),
            _make("r4"),
        ])

    assert [id(r) for r in pool.resources] == [id(r) for r in old]
    assert [r.total_requests for r in pool.resources] == [5, 6, 7]
    assert pool._cursor == 2


# 11) Provider-specific Resource subclasses must survive reconcile, with all
#     of their provider-specific fields and credential material intact.
async def test_reconcile_preserves_provider_specific_resource_type(fake_clock):
    cooldown: CooldownManager = make_cooldown(fake_clock)
    deadline = _cooldown_until(fake_clock)
    old = GeminiCliResource(
        id="acc-01",
        provider="gemini_cli",
        access_token="old-access",
        refresh_token="old-refresh",
        project_id="old-project",
        tier="FREE",
    )
    old.total_failures = 7
    old.consecutive_failures = 2
    old.total_requests = 19
    old.health = HealthState.DEGRADED
    old.cooldown_until = deadline

    pool = InMemoryPool(provider="gemini_cli", resources=[old], cooldown=cooldown)

    new = GeminiCliResource(
        id="acc-01",
        provider="gemini_cli",
        access_token="new-access",
        refresh_token="new-refresh",
        project_id="new-project",
        tier="PRO",
        pinned_model="new-model",
    )
    await pool.reconcile_resources([new])

    merged = pool.resources[0]
    # The concrete type is preserved, not degraded to core.Resource.
    assert isinstance(merged, GeminiCliResource)
    assert type(merged) is type(new)
    # Provider-specific configuration and credential material come from new.
    assert merged.access_token == "new-access"
    assert merged.refresh_token == "new-refresh"
    assert merged.project_id == "new-project"
    assert merged.tier == "PRO"
    assert merged.pinned_model == "new-model"
    # Runtime state is inherited from the old object.
    assert merged.health is HealthState.DEGRADED
    assert merged.cooldown_until == deadline
    assert merged.total_failures == 7
    assert merged.consecutive_failures == 2
    assert merged.total_requests == 19
    # in_flight is never inherited.
    assert merged.in_flight == 0
    # The caller's object is neither mutated nor shared.
    assert merged is not new
    assert new.total_failures == 0
    assert new.health is HealthState.HEALTHY
    assert new.cooldown_until is None
    merged.pinned_model = "mutated-after-reconcile"
    assert new.pinned_model == "new-model"


async def test_reconcile_does_not_inherit_in_flight(fake_clock):
    pool = make_pool(fake_clock, make_resources([{"id": "r1"}]))
    old_r1 = pool.resources[0]
    old_r1.total_requests = 4

    await pool.reconcile_resources([_make("r1")])

    assert pool.resources[0].in_flight == 0
    assert pool.resources[0].total_requests == 4


# 12) Deep copy must also detach *mutable* provider-specific state, so the
#     pool never shares caller-owned containers (and vice versa).
class _MutableGeminiCliResource(GeminiCliResource):
    """GeminiCliResource plus a mutable provider-specific container."""

    labels: list[str] = []


async def test_reconcile_does_not_share_mutable_provider_fields(fake_clock):
    cooldown: CooldownManager = make_cooldown(fake_clock)
    old = _MutableGeminiCliResource(
        id="acc-01",
        provider="gemini_cli",
        tier="FREE",
        labels=["old-a", "old-b"],
    )
    old.total_failures = 7
    pool = InMemoryPool(provider="gemini_cli", resources=[old], cooldown=cooldown)

    new = _MutableGeminiCliResource(
        id="acc-01",
        provider="gemini_cli",
        tier="PRO",
        labels=["new-a"],
    )
    await pool.reconcile_resources([new])

    merged = pool.resources[0]
    # Concrete type is preserved.
    assert type(merged) is _MutableGeminiCliResource
    # Runtime state is inherited, provider config comes from `new`.
    assert merged.total_failures == 7
    assert merged.tier == "PRO"
    assert merged.labels == ["new-a"]

    # Mutable container is not shared in either direction.
    assert merged.labels is not new.labels
    merged.labels.append("mutated-after-reconcile")
    assert new.labels == ["new-a"]
    new.labels.append("caller-side")
    assert merged.labels == ["new-a", "mutated-after-reconcile"]
