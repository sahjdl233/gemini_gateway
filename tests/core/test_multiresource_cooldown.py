"""Additional multi-resource cooldown edge cases."""

from __future__ import annotations

from core.pool import InMemoryPool

from tests.conftest import make_pool, make_resources


async def test_independent_cooldown_per_resource(fake_clock):
    """Two resources should have independent cooldown states."""
    pool = make_pool(
        fake_clock,
        make_resources([
            {"id": "r1", "scenario": "rate_limit", "retry_after": 10.0},
            {"id": "r2", "scenario": "success", "reply_text": "R2-OK"},
        ]),
    )
    r1 = pool.resources[0]
    r2 = pool.resources[1]
    
    # Trigger cooldown on r1 only
    await pool.record_rate_limit(r1, retry_after=10.0)
    
    # r1 should be in cooldown, r2 should not
    assert pool._cooldown.in_cooldown(r1)
    assert not pool._cooldown.in_cooldown(r2)
    
    # Acquiring should skip r1 and return r2
    res = await pool.acquire()
    assert res is not None
    assert res.id == "r2"


async def test_all_resources_cooldown_returns_none(fake_clock):
    """When all resources are in cooldown, acquire returns None."""
    pool = make_pool(
        fake_clock,
        make_resources([
            {"id": "r1", "scenario": "rate_limit", "retry_after": 10.0},
            {"id": "r2", "scenario": "rate_limit", "retry_after": 10.0},
        ]),
    )
    
    # Cooldown both resources
    await pool.record_rate_limit(pool.resources[0], retry_after=10.0)
    await pool.record_rate_limit(pool.resources[1], retry_after=10.0)
    
    # No resources available
    res = await pool.acquire()
    assert res is None
    
    # has_available should be False
    available = await pool.has_available()
    assert not available


async def test_cooldown_expiration_allows_retry(fake_clock):
    """After cooldown expires, resource becomes available again."""
    pool = make_pool(
        fake_clock,
        make_resources([
            {"id": "r1", "scenario": "rate_limit", "retry_after": 1.0},
        ]),
    )
    r1 = pool.resources[0]
    
    # Trigger cooldown
    await pool.record_rate_limit(r1, retry_after=1.0)
    assert pool._cooldown.in_cooldown(r1)
    
    # Advance time past cooldown
    fake_clock.advance(1.5)
    
    # Should now be available
    assert not pool._cooldown.in_cooldown(r1)
    res = await pool.acquire()
    assert res is not None
    assert res.id == "r1"


async def test_consecutive_failures_independent(fake_clock):
    """Consecutive failures counter is independent per resource."""
    pool = make_pool(
        fake_clock,
        make_resources([
            {"id": "r1", "scenario": "server_error"},
            {"id": "r2", "scenario": "success"},
        ]),
    )
    
    # Fail r1 multiple times
    for _ in range(3):
        await pool.record_failure(pool.resources[0], Exception("fail"))
    
    # r1 should have consecutive_failures=3
    assert pool.resources[0].consecutive_failures == 3
    
    # r2 should still be at 0
    assert pool.resources[1].consecutive_failures == 0


async def test_success_resets_consecutive_failures(fake_clock):
    """Successful request resets consecutive_failures."""
    pool = make_pool(
        fake_clock,
        make_resources([
            {"id": "r1", "scenario": "server_error"},
            {"id": "r2", "scenario": "success", "reply_text": "OK"},
        ]),
    )
    
    # Fail twice
    await pool.record_failure(pool.resources[0], Exception("fail1"))
    await pool.record_failure(pool.resources[0], Exception("fail2"))
    assert pool.resources[0].consecutive_failures == 2
    
    # Success resets counter
    await pool.record_success(pool.resources[0])
    assert pool.resources[0].consecutive_failures == 0