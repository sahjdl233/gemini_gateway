"""Cooldown / failover with ResourcePool (TASK-008)."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.cooldown import CooldownManager
from core.errors import RateLimitError
from core.pool import InMemoryPool
from core.resource import Resource
from core.scheduler import Scheduler
from providers.gemini_cli.provider import GeminiCliProvider
from providers.gemini_cli.resource import GeminiCliResource
from tests.conftest import FakeClock


async def test_rate_limit_triggers_cooldown():
    clock = FakeClock()
    cooldown = CooldownManager(now_fn=lambda: clock.now)
    r1 = GeminiCliResource(
        id="a1",
        provider="gemini_cli",
        refresh_token="rt1",
        client_id="cid1",
        client_secret="cs1",
        project_id="p1",
    )
    r2 = GeminiCliResource(
        id="a2",
        provider="gemini_cli",
        refresh_token="rt2",
        client_id="cid2",
        client_secret="cs2",
        project_id="p2",
    )
    pool = InMemoryPool(
        provider="gemini_cli",
        resources=[r1, r2],
        cooldown=cooldown,
    )

    # acquire the first eligible resource (round-robin may start at either)
    acquired = await pool.acquire()
    assert acquired is not None
    assert acquired in (r1, r2)
    other = r2 if acquired is r1 else r1

    # simulate 429 with retry_after = 10s
    await pool.record_rate_limit(acquired, retry_after=10.0)
    assert cooldown.in_cooldown(acquired)

    # next acquire should skip the cooled-down resource and return the other
    nxt = await pool.acquire()
    assert nxt is not None
    assert nxt is other
    assert cooldown.in_cooldown(acquired)
    assert not cooldown.in_cooldown(other)


async def test_scheduler_fallback_on_429():
    clock = FakeClock()
    cooldown = CooldownManager(now_fn=lambda: clock.now)
    pool = InMemoryPool(
        provider="gemini_cli",
        resources=[
            GeminiCliResource(
                id="a1", provider="gemini_cli", refresh_token="rt", client_id="cid",
                client_secret="cs", project_id="p1",
            ),
            GeminiCliResource(
                id="a2", provider="gemini_cli", refresh_token="rt", client_id="cid",
                client_secret="cs", project_id="p2",
            ),
        ],
        cooldown=cooldown,
    )

    # Fake provider that raises RateLimitError on first resource, succeeds on second
    class FakeProvider(GeminiCliProvider):
        def __init__(self):
            super().__init__(models=["gemini-2.5-flash"])
            self.call_count = 0

        async def complete(self, request, resource):
            self.call_count += 1
            if self.call_count == 1:
                raise RateLimitError("quota", provider="gemini_cli", resource_id=resource.id, scope="resource", retry_after=1.0)
            from core.models import ChatResponse, Usage
            return ChatResponse(id="x", model="gemini-2.5-flash", text="ok", usage=Usage())

    provider = FakeProvider()
    # monkeypatch scheduler with our provider
    from core.model_registry import ModelRegistry
    model_registry = ModelRegistry(providers={"gemini_cli": provider})
    scheduler = Scheduler(providers={"gemini_cli": provider}, pools={"gemini_cli": pool}, model_registry=model_registry, max_retries=1)

    from core.models import ChatRequest, ChatMessage
    req = ChatRequest(model="gemini-2.5-flash", messages=[ChatMessage(role="user", content="hi")])
    resp = await scheduler.chat_completion(req)

    assert resp.text == "ok"
    # should have tried a1 (fail), then a2 (success)
    assert provider.call_count == 2
