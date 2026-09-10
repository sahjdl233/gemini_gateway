"""TASK-001 integration scenarios A-D: multi-resource & multi-provider."""

from __future__ import annotations

import asyncio

import pytest

from core.errors import RateLimitError
from core.model_registry import ModelRegistry
from core.scheduler import Scheduler
from providers.fake import FakeProvider

from tests.conftest import make_chat_request, make_cooldown, make_multi_provider_scheduler, make_pool, make_resources


class CountingProvider(FakeProvider):
    def __init__(self, provider_id: str) -> None:
        super().__init__()
        self.provider_id = provider_id
        self.list_calls = 0

    async def list_models(self):
        self.list_calls += 1
        return await super().list_models()


def _scheduler_from_specs(clock, provider_specs, max_retries=2, refresh_interval=300.0):
    """Build scheduler with multiple providers, each with its own pool."""
    providers = {}
    pools = {}
    for spec in provider_specs:
        pid = spec["id"]
        provider = CountingProvider(pid)
        resources = make_resources(spec["resources"])
        for r in resources:
            r.provider = pid
        pool = make_pool(clock, resources)
        providers[pid] = provider
        pools[pid] = pool
    registry = ModelRegistry(providers=providers, refresh_interval=refresh_interval)
    return Scheduler(
        providers=providers,
        pools=pools,
        model_registry=registry,
        max_retries=max_retries,
    )


async def test_scenario_a_single_provider_fallback(fake_clock):
    """Provider A: A1 -> 429, A2 -> success; request completed by A2."""
    scheduler = _scheduler_from_specs(
        fake_clock,
        [
            {
                "id": "fake-a",
                "resources": [
                    {"id": "A1", "scenario": "rate_limit", "retry_after": 1.0},
                    {"id": "A2", "reply_text": "A2-REPLY"},
                ],
            }
        ],
    )
    # Force A1 to be selected first so the 429 -> fallback path is exercised.
    scheduler.pools["fake-a"]._cursor = len(scheduler.pools["fake-a"].resources) - 1
    resp = await scheduler.chat_completion(make_chat_request())
    assert resp.text == "A2-REPLY"
    pool = scheduler.pools["fake-a"]
    a1 = [r for r in pool.resources if r.id == "A1"][0]
    a2 = [r for r in pool.resources if r.id == "A2"][0]
    assert a1.total_failures == 1
    assert a1.health.name == "COOLDOWN"
    assert a2.total_requests == 1


async def test_scenario_b_cross_provider_fallback(fake_clock):
    """Provider A: A1 -> 429; Provider B: B1 -> success; fallback to B1."""
    scheduler = _scheduler_from_specs(
        fake_clock,
        [
            {
                "id": "fake-a",
                "resources": [{"id": "A1", "scenario": "rate_limit", "retry_after": 1.0}],
            },
            {
                "id": "fake-b",
                "resources": [{"id": "B1", "reply_text": "B1-REPLY"}],
            },
        ],
    )
    resp = await scheduler.chat_completion(make_chat_request())
    assert resp.text == "B1-REPLY"
    b_pool = scheduler.pools["fake-b"]
    b1 = b_pool.resources[0]
    assert b1.total_requests == 1


async def test_scenario_c_same_local_id_distinct_resources(fake_clock):
    """firebase/project-01 and vertex/project-01 are two different resources."""
    from core.resource import ResourceKey

    scheduler = _scheduler_from_specs(
        fake_clock,
        [
            {
                "id": "firebase",
                "resources": [{"id": "project-01", "scenario": "rate_limit", "retry_after": 1.0}],
            },
            {
                "id": "vertex",
                "resources": [{"id": "project-01", "reply_text": "VERTEX-REPLY"}],
            },
        ],
    )
    resp = await scheduler.chat_completion(make_chat_request())
    assert resp.text == "VERTEX-REPLY"
    # the two resources must have distinct ResourceKeys despite same id
    fb = scheduler.pools["firebase"].resources[0]
    vx = scheduler.pools["vertex"].resources[0]
    assert fb.resource_key != vx.resource_key
    assert fb.resource_key == ResourceKey(provider="firebase", id="project-01")
    assert vx.resource_key == ResourceKey(provider="vertex", id="project-01")


async def test_scenario_d_concurrent_refresh_single_flight(fake_clock):
    """Multiple requests on expired registry => only one effective refresh."""
    scheduler = _scheduler_from_specs(
        fake_clock,
        [
            {"id": "fake", "resources": [{"id": "r1", "reply_text": "REPLY"}]},
        ],
        refresh_interval=300.0,
    )
    provider = scheduler.providers["fake"]
    # Warm up so the index is built, then force expiry.
    await scheduler.chat_completion(make_chat_request())
    calls_before = provider.list_calls
    # Force expiry with a clearly-past monotonic timestamp (TASK-002-FIX-02:
    # the 0.0 reset is the fragile pattern this task removes).
    scheduler.model_registry._last_refresh = (
        scheduler.model_registry._last_refresh
        - scheduler.model_registry.refresh_interval
        - 5.0
    )

    results = await asyncio.gather(
        *[scheduler.chat_completion(make_chat_request()) for _ in range(5)]
    )
    for resp in results:
        assert resp.text == "REPLY"
    # All concurrent queries collapsed into a single refresh.
    assert provider.list_calls == calls_before + 1
