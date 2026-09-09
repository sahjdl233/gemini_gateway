"""Scheduler hardening tests (TASK-001 tasks 5-7)."""

from __future__ import annotations

import pytest

from core.errors import (
    AuthenticationError,
    AuthorizationError,
    RateLimitError,
    TimeoutError,
    UpstreamUnavailableError,
)
from core.pool import InMemoryPool
from core.scheduler import Scheduler
from providers.fake import FakeProvider

from tests.conftest import make_chat_request, make_cooldown, make_resources


def _scheduler(clock, resources, max_retries=2):
    pool = InMemoryPool(
        provider="fake", resources=resources, cooldown=make_cooldown(clock)
    )
    return Scheduler(
        providers={"fake": FakeProvider()},
        pools={"fake": pool},
        max_retries=max_retries,
    ), pool


async def test_401_not_converted_to_429(fake_clock):
    scheduler, pool = _scheduler(
        fake_clock, make_resources([{"id": "r1", "scenario": "auth_error"}])
    )
    with pytest.raises(AuthenticationError):
        await scheduler.chat_completion(make_chat_request())
    # AuthenticationError is not a RateLimitError and is not retryable
    r1 = pool.resources[0]
    assert r1.total_failures == 1


async def test_403_not_converted_to_429(fake_clock):
    scheduler, pool = _scheduler(
        fake_clock, make_resources([{"id": "r1", "scenario": "authz_error"}])
    )
    with pytest.raises(AuthorizationError):
        await scheduler.chat_completion(make_chat_request())
    r1 = pool.resources[0]
    assert r1.total_failures == 1


async def test_timeout_retryable_and_falls_back(fake_clock):
    scheduler, pool = _scheduler(
        fake_clock,
        make_resources([
            {"id": "r1", "scenario": "timeout"},
            {"id": "r2", "reply_text": "R2-REPLY"},
        ]),
        max_retries=3,
    )
    # force first
    pool._cursor = len(pool.resources) - 1
    resp = await scheduler.chat_completion(make_chat_request())
    assert resp.text == "R2-REPLY"


async def test_500_retryable(fake_clock):
    scheduler, pool = _scheduler(
        fake_clock,
        make_resources([
            {"id": "r1", "scenario": "server_error"},
            {"id": "r2", "reply_text": "R2-REPLY"},
        ]),
        max_retries=3,
    )
    pool._cursor = len(pool.resources) - 1
    resp = await scheduler.chat_completion(make_chat_request())
    assert resp.text == "R2-REPLY"


async def test_max_retries_bounds_retries(fake_clock):
    """With max_retries=0, a failing single resource does not loop forever."""
    scheduler, pool = _scheduler(
        fake_clock,
        make_resources([{"id": "r1", "scenario": "server_error"}]),
        max_retries=0,
    )
    with pytest.raises(UpstreamUnavailableError):
        await scheduler.chat_completion(make_chat_request())
    assert pool.resources[0].total_requests == 1


async def test_disabled_resource_not_selected(fake_clock):
    from core.health import HealthState

    resources = make_resources([
        {"id": "r1", "scenario": "server_error"},
        {"id": "r2", "reply_text": "R2-REPLY"},
    ])
    resources[0].health = HealthState.DISABLED
    scheduler, pool = _scheduler(fake_clock, resources)
    resp = await scheduler.chat_completion(make_chat_request())
    assert resp.text == "R2-REPLY"
    assert resources[0].total_requests == 0
