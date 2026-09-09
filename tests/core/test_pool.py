"""ResourcePool (InMemoryPool) tests."""

from __future__ import annotations

from core.health import HealthState
from core.errors import RateLimitError

from tests.conftest import make_pool, make_resources


async def test_acquire_release_in_flight(fake_clock):
    pool = make_pool(fake_clock, make_resources([{"id": "r1"}, {"id": "r2"}]))
    res = await pool.acquire()
    assert res is not None
    assert res.in_flight == 1
    await pool.release(res)
    assert res.in_flight == 0


async def test_round_robin_alternates(fake_clock):
    pool = make_pool(fake_clock, make_resources([{"id": "r1"}, {"id": "r2"}]))
    first = await pool.acquire()
    second = await pool.acquire()
    third = await pool.acquire()
    await pool.release(first)
    await pool.release(second)
    await pool.release(third)
    assert first.id == "r2"
    assert second.id == "r1"
    assert third.id == "r2"


async def test_rate_limited_resource_skipped(fake_clock):
    pool = make_pool(
        fake_clock, make_resources([{"id": "r1", "scenario": "rate_limit", "retry_after": 1.0}, {"id": "r2"}])
    )
    r1 = pool.resources[0]
    await pool.record_rate_limit(r1, retry_after=1.0)
    res = await pool.acquire()
    assert res is not None
    assert res.id == "r2"
    fake_clock.advance(1.2)
    res = await pool.acquire()
    assert res is not None
    assert res.id in ("r1", "r2")


async def test_disabled_resource_not_acquired(fake_clock):
    from core.resource import Resource

    specs = make_resources([{"id": "r1"}, {"id": "r2"}])
    specs[0].enabled = False
    pool = make_pool(fake_clock, specs)
    res = await pool.acquire()
    assert res is not None
    assert res.id == "r2"


async def test_no_available_returns_none(fake_clock):
    from core.resource import Resource

    specs = make_resources([{"id": "r1"}, {"id": "r2"}])
    for r in specs:
        r.enabled = False
    pool = make_pool(fake_clock, specs)
    assert await pool.acquire() is None
    assert await pool.has_available() is False


async def test_record_metrics(fake_clock):
    pool = make_pool(fake_clock, make_resources([{"id": "r1"}]))
    res = pool.resources[0]
    await pool.record_success(res)
    assert res.total_requests == 1
    assert res.total_failures == 0
    await pool.record_rate_limit(res, retry_after=0.5)
    assert res.total_requests == 2
    assert res.total_failures == 1
    assert res.health == HealthState.COOLDOWN
