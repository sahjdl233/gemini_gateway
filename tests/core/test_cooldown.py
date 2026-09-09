"""Cooldown tests: Retry-After, exponential backoff + jitter, recovery."""

from __future__ import annotations

from core.health import HealthState
from providers.fake import FakeResource

from tests.conftest import make_cooldown, make_resources


def test_retry_after_honored(fake_clock):
    [res] = make_resources([{"id": "r1", "scenario": "rate_limit", "retry_after": 3.0}])
    cd = make_cooldown(fake_clock)
    cd.apply_rate_limit(res, retry_after=3.0)
    assert res.health == HealthState.COOLDOWN
    assert res.consecutive_failures == 1
    assert cd.in_cooldown(res) is True
    fake_clock.advance(2.0)
    assert cd.in_cooldown(res) is True
    fake_clock.advance(1.5)
    assert cd.in_cooldown(res) is False
    assert res.health == HealthState.HEALTHY


def test_exponential_backoff_grows(fake_clock):
    [res] = make_resources([{"id": "r1"}])
    cd = make_cooldown(fake_clock, base_delay=1.0, factor=2.0, max_delay=64.0, jitter=0.0)
    cd.apply_rate_limit(res)
    first = (res.cooldown_until - fake_clock.now).total_seconds()
    assert first == 1.0
    fake_clock.advance(first)
    cd.apply_rate_limit(res)
    second = (res.cooldown_until - fake_clock.now).total_seconds()
    assert second == 2.0
    fake_clock.advance(second)
    cd.apply_rate_limit(res)
    third = (res.cooldown_until - fake_clock.now).total_seconds()
    assert third == 4.0


def test_jitter_applied(fake_clock):
    import random

    random.seed(1234)
    [res] = make_resources([{"id": "r1"}])
    cd = make_cooldown(fake_clock, base_delay=10.0, jitter=0.5)
    cd.apply_rate_limit(res)
    delay = (res.cooldown_until - fake_clock.now).total_seconds()
    assert 10.0 <= delay <= 15.0


def test_failure_degrades_then_cooldowns(fake_clock):
    [res] = make_resources([{"id": "r1"}])
    cd = make_cooldown(fake_clock, degrade_threshold=3, jitter=0.0)
    cd.apply_failure(res)
    assert res.health == HealthState.DEGRADED
    cd.apply_failure(res)
    assert res.health == HealthState.DEGRADED
    cd.apply_failure(res)
    assert res.health == HealthState.COOLDOWN
    assert cd.in_cooldown(res) is True


def test_reset_after_success(fake_clock):
    [res] = make_resources([{"id": "r1"}])
    cd = make_cooldown(fake_clock, degrade_threshold=2)
    cd.apply_failure(res)
    cd.apply_failure(res)
    assert res.health == HealthState.COOLDOWN
    cd.reset(res)
    assert res.health == HealthState.HEALTHY
    assert res.consecutive_failures == 0
    assert res.cooldown_until is None


def test_no_hardcoded_sleep(fake_clock):
    [res] = make_resources([{"id": "r1"}])
    cd = make_cooldown(fake_clock)
    cd.apply_rate_limit(res)
    delay = (res.cooldown_until - fake_clock.now).total_seconds()
    assert 0 < delay <= 10.0 * 1.1

