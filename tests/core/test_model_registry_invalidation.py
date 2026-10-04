"""CONFIG-R-2-B: ModelRegistry invalidation contract.

Defines how the model index may be refreshed OUTSIDE the TTL when a
control-plane change makes it potentially stale — and where the boundary
sits:

* ``registry.invalidate()`` is lazy (no provider call), state-preserving
  (last-known-good discovery state untouched), idempotent, and forces
  exactly one rebuild on the next query.
* Runtime scheduling state (health / cooldown / retry counters /
  in-flight) must never invalidate the index — the scheduler's runtime
  state is not part of the model index, and this test pins that no
  future scheduler-health hook may call invalidate().

No event bus, no repository callbacks, no observer pattern: the caller
is a control-plane component that explicitly calls invalidate() after a
definition mutation (docs/CONFIG-R2B-MODEL-REGISTRY-INVALIDATION.md).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.errors import ProviderError
from core.health import HealthState
from core.models import ModelInfo
from core.model_registry import ModelRegistry
from core.pool import InMemoryPool
from core.resource import Resource
from providers.fake import FakeProvider


class ScriptedProvider(FakeProvider):
    """FakeProvider whose list_models() follows a script of results.

    Each entry is either a list of model ids (success) or an exception
    instance (failure).  The last entry repeats once exhausted; calls
    are counted so tests can assert exactly when Discovery ran.
    """

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        self.list_calls = 0

    async def list_models(self):
        self.list_calls += 1
        step = self.script[min(self.list_calls - 1, len(self.script) - 1)]
        if isinstance(step, BaseException):
            raise step
        return [ModelInfo(id=mid, provider="scripted") for mid in step]


async def _model_ids(registry: ModelRegistry) -> set:
    return {info.id for info in await registry.list_models()}


def _soon() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=60)


# -- Case A: invalidate forces a fresh discovery, independent of the TTL ----------


async def test_invalidate_forces_next_query_to_rediscover():
    provider = ScriptedProvider([["model-a"], ["model-b"]])
    registry = ModelRegistry(
        providers={"scripted": provider}, refresh_interval=3600.0
    )

    # Warm the cache (call 1).
    assert await _model_ids(registry) == {"model-a"}
    # The provider's answer changes — under TTL semantics the registry
    # would keep serving the stale index for the whole interval.
    assert await _model_ids(registry) == {"model-a"}
    assert provider.list_calls == 1

    # Control-plane definition change happened: invalidate...
    registry.invalidate()
    # ...and the very next query rediscovers — no TTL backdating needed.
    assert await _model_ids(registry) == {"model-b"}
    assert provider.list_calls == 2


async def test_invalidate_does_not_refresh_by_itself():
    """Lazy contract: invalidate() marks stale; it never calls providers."""
    provider = ScriptedProvider([["model-a"]])
    registry = ModelRegistry(
        providers={"scripted": provider}, refresh_interval=3600.0
    )
    assert await _model_ids(registry) == {"model-a"}
    assert provider.list_calls == 1

    registry.invalidate()
    registry.invalidate()
    assert provider.list_calls == 1

    # Exactly ONE discovery serves the next query (not three).
    assert await _model_ids(registry) == {"model-a"}
    assert provider.list_calls == 2


# -- Case B: invalidate preserves provider discovery state ------------------------


async def test_invalidate_preserves_failure_tracking_and_last_known_good():
    provider = ScriptedProvider(
        [
            ["model-a"],            # 1: warm success
            ProviderError("boom"),  # 2: failure while index stays warm
            ["model-b"],            # 3: post-invalidate recovery
        ]
    )
    registry = ModelRegistry(
        providers={"scripted": provider}, refresh_interval=3600.0
    )
    assert await _model_ids(registry) == {"model-a"}

    # A failing discovery (manual refresh) keeps the last known good...
    await registry.refresh()
    assert await _model_ids(registry) == {"model-a"}
    status = registry.provider_status("scripted")
    assert status["failure_count"] == 1
    assert status["discovered"] is True

    # ...and invalidation neither resets that tracking nor invents state.
    registry.invalidate()
    status_after = registry.provider_status("scripted")
    assert status_after["failure_count"] == 1
    assert status_after["discovered"] is True

    # The post-invalidate refresh recovers and updates the index.
    assert await _model_ids(registry) == {"model-b"}
    assert registry.provider_status("scripted")["failure_count"] == 1


# -- Case C: repeated invalidation is safe ----------------------------------------


async def test_repeated_invalidation_is_safe():
    provider = ScriptedProvider([["model-a"]])
    registry = ModelRegistry(
        providers={"scripted": provider}, refresh_interval=3600.0
    )
    assert await _model_ids(registry) == {"model-a"}

    registry.invalidate()
    registry.invalidate()
    registry.invalidate()  # no exception, no extra provider work

    assert await _model_ids(registry) == {"model-a"}
    assert provider.list_calls == 2  # warm + exactly one post-invalidate


# -- Task 3: runtime scheduling state is NOT an invalidation trigger ---------------


async def test_runtime_state_changes_never_invalidate_the_index():
    """The boundary: pool runtime state (health / cooldown / failure
    counters / in-flight) is invisible to the model index.  If a future
    scheduler-health hook ever calls invalidate(), the freshness marker
    below would reset and this test would fail."""
    provider = ScriptedProvider([["model-a"]])
    registry = ModelRegistry(
        providers={"scripted": provider}, refresh_interval=3600.0
    )
    pool = InMemoryPool(
        provider="fake",
        resources=[
            Resource(id="r1", provider="fake", enabled=True),
        ],
        cooldown=None,
    )

    assert await _model_ids(registry) == {"model-a"}
    warmed_at = registry.last_refresh

    # Runtime-only mutations on pool resources: scheduling state, never
    # definition state.
    resource = pool.resources[0]
    resource.health = HealthState.DEGRADED
    resource.cooldown_until = _soon()
    resource.consecutive_failures = 5
    resource.total_requests = 99
    resource.total_failures = 7
    resource.in_flight = 2

    # No invalidation happened: the freshness marker is untouched...
    assert registry.last_refresh == warmed_at
    assert provider.list_calls == 1
    # ...the passive snapshot is unchanged (index shape: model -> providers)...
    snapshot = registry.snapshot()
    assert snapshot.get("model-a") == ["scripted"]
    assert "model-b" not in snapshot
    # ...and even an explicit refresh yields the same index: runtime
    # state is not an input to Discovery output.
    await registry.refresh()
    assert await _model_ids(registry) == {"model-a"}


# -- Task 4: invalidate composes with manual refresh -------------------------------


async def test_invalidate_composes_with_manual_refresh():
    provider = ScriptedProvider([["model-a"], ["model-b"], ["model-c"]])
    registry = ModelRegistry(
        providers={"scripted": provider}, refresh_interval=3600.0
    )

    # Manual refresh keeps working exactly as before.
    assert await _model_ids(registry) == {"model-a"}

    # invalidate() marks stale (freshness marker cleared)...
    registry.invalidate()
    assert registry.last_refresh is None

    # ...the next manual refresh rebuilds fresh, and invalidate() after
    # it marks stale again — the two compose without interference.
    assert await _model_ids(registry) == {"model-b"}
    registry.invalidate()
    assert registry.last_refresh is None
    assert await _model_ids(registry) == {"model-c"}
