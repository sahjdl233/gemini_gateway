"""Scheduler tests: selection, cooldown exclusion, retry, fallback, 429."""

from __future__ import annotations

import pytest

from core.scheduler import Scheduler
from core.errors import (
    AuthenticationError,
    ModelNotFoundError,
    RateLimitError,
    TimeoutError,
    UpstreamUnavailableError,
)
from core.pool import InMemoryPool
from providers.fake import FakeProvider

from tests.conftest import make_chat_request, make_cooldown, make_resources


async def _build(pools, max_retries=2):
    provider = FakeProvider()
    return Scheduler(
        providers={"fake": provider},
        pools={"fake": pools[0]},
        max_retries=max_retries,
    )


def _force_first(pool):
    # Make the next acquire() return resources[0]; the pool advances the
    # cursor on each acquire, so rewind it for deterministic tests.
    pool._cursor = len(pool.resources) - 1


class CountingProvider(FakeProvider):
    def __init__(self):
        self.calls: list[str] = []

    async def complete(self, request, resource):
        self.calls.append(resource.id)
        return await super().complete(request, resource)

    async def stream(self, request, resource):
        self.calls.append(resource.id)
        async for chunk in super().stream(request, resource):
            yield chunk


async def test_retryable_resource_failure_falls_back_without_immediate_retry(fake_clock):
    provider = CountingProvider()
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([
            {"id": "r2", "scenario": "server_error"},
            {"id": "r3", "reply_text": "R3-REPLY"},
        ]),
        cooldown=make_cooldown(fake_clock),
    )
    # Force the failing resource first even if the successful fallback has
    # lower in_flight.
    _force_first(pool)
    scheduler = Scheduler(
        providers={"fake": provider},
        pools={"fake": pool},
        max_retries=2,
    )

    resp = await scheduler.chat_completion(make_chat_request())

    assert resp.text == "R3-REPLY"
    assert provider.calls == ["r2", "r3"]
    assert pool.resources[0].total_failures == 1
    assert pool.resources[1].total_requests == 1


async def test_all_retryable_failures_after_fallback_returns_last_error(fake_clock):
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([
            {"id": "r1", "scenario": "timeout"},
            {"id": "r2", "scenario": "timeout"},
        ]),
        cooldown=make_cooldown(fake_clock),
    )
    scheduler = await _build([pool])
    _force_first(pool)

    with pytest.raises(TimeoutError):
        await scheduler.chat_completion(make_chat_request())

    assert [r.total_failures for r in pool.resources] == [1, 1]
    assert [r.total_requests for r in pool.resources] == [1, 1]


async def test_non_retryable_error_does_not_consume_retry_budget(fake_clock):
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([
            {"id": "r1", "scenario": "auth_error"},
            {"id": "r2", "reply_text": "R2-REPLY"},
        ]),
        cooldown=make_cooldown(fake_clock),
    )
    scheduler = await _build([pool])
    _force_first(pool)

    with pytest.raises(AuthenticationError):
        await scheduler.chat_completion(make_chat_request())

    assert pool.resources[0].total_failures == 1
    assert pool.resources[1].total_requests == 0


async def test_stream_retryable_failure_before_first_chunk_falls_back(fake_clock):
    provider = CountingProvider()
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([
            {"id": "r2", "scenario": "timeout"},
            {"id": "r3", "reply_text": "R3-STREAM"},
        ]),
        cooldown=make_cooldown(fake_clock),
    )
    _force_first(pool)
    scheduler = Scheduler(
        providers={"fake": provider},
        pools={"fake": pool},
        max_retries=2,
    )

    chunks = [chunk async for chunk in scheduler.stream_chat(make_chat_request())]

    assert len(chunks) == 1
    assert chunks[0].content == "R3-STREAM"
    assert provider.calls == ["r2", "r3"]


async def test_retryable_resource_failure_uses_load_aware_pool_selection(fake_clock):
    provider = CountingProvider()
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([
            {"id": "r1", "reply_text": "R1-REPLY"},
            {"id": "r2", "reply_text": "R2-REPLY"},
        ]),
        cooldown=make_cooldown(fake_clock),
    )
    # r1 is lower loaded, so a plain round-robin would choose r1 first.
    pool.resources[0].in_flight = 0
    pool.resources[1].in_flight = 1
    scheduler = Scheduler(
        providers={"fake": provider},
        pools={"fake": pool},
        max_retries=2,
    )

    with pytest.raises(TimeoutError):
        await scheduler.chat_completion(make_chat_request())

    assert provider.calls == ["r1"]
    assert pool.resources[0].total_failures == 1
    assert pool.resources[1].total_requests == 0
    assert pool.resources[1].total_failures == 1
    assert pool.resources[0].total_requests == 0


async def test_success_uses_resource(fake_clock):
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([{"id": "r1", "reply_text": "R1-REPLY"}]),
        cooldown=make_cooldown(fake_clock),
    )
    scheduler = await _build([pool])
    _force_first(pool)
    resp = await scheduler.chat_completion(make_chat_request())
    assert resp.text == "R1-REPLY"
    r1 = pool.resources[0]
    assert r1.total_requests == 1
    assert r1.total_failures == 0


async def test_rate_limit_falls_back_to_next(fake_clock):
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([
            {"id": "r1", "scenario": "rate_limit", "retry_after": 2.0},
            {"id": "r2", "reply_text": "R2-REPLY"},
        ]),
        cooldown=make_cooldown(fake_clock),
    )
    scheduler = await _build([pool])
    _force_first(pool)
    resp = await scheduler.chat_completion(make_chat_request())
    assert resp.text == "R2-REPLY"
    assert pool.resources[0].health.name == "COOLDOWN"
    assert pool.resources[1].total_requests >= 1


async def test_cooldown_resource_not_selected_again(fake_clock):
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([
            {"id": "r1", "scenario": "rate_limit", "retry_after": 5.0},
            {"id": "r2", "reply_text": "R2-REPLY"},
        ]),
        cooldown=make_cooldown(fake_clock),
    )
    scheduler = await _build([pool])
    _force_first(pool)
    await scheduler.chat_completion(make_chat_request())
    before = pool.resources[0].total_requests
    fake_clock.advance(1.0)
    await scheduler.chat_completion(make_chat_request())
    assert pool.resources[0].total_requests == before  # still cooling down


async def test_all_rate_limited_raises_429(fake_clock):
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([
            {"id": "r1", "scenario": "rate_limit", "retry_after": 1.0},
            {"id": "r2", "scenario": "rate_limit", "retry_after": 1.0},
        ]),
        cooldown=make_cooldown(fake_clock),
    )
    scheduler = await _build([pool])
    with pytest.raises(RateLimitError):
        await scheduler.chat_completion(make_chat_request())
    assert pool.resources[0].health.name == "COOLDOWN"
    assert pool.resources[1].health.name == "COOLDOWN"


async def test_429_not_treated_as_permanent(fake_clock):
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([
            {"id": "r1", "scenario": "rate_limit", "retry_after": 1.0},
        ]),
        cooldown=make_cooldown(fake_clock),
    )
    scheduler = await _build([pool])
    with pytest.raises(RateLimitError):
        await scheduler.chat_completion(make_chat_request())
    fake_clock.advance(10.0)
    # r1 recovers; a new attempt still hits the fake 429, but the resource is usable again
    with pytest.raises(RateLimitError):
        await scheduler.chat_completion(make_chat_request())
    assert pool.resources[0].total_requests == 2


async def test_auth_error_not_retryable(fake_clock):
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([{"id": "r1", "scenario": "auth_error"}]),
        cooldown=make_cooldown(fake_clock),
    )
    scheduler = await _build([pool])
    with pytest.raises(AuthenticationError):
        await scheduler.chat_completion(make_chat_request())


async def test_timeout_retries_then_falls_back(fake_clock):
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([
            {"id": "r1", "scenario": "timeout"},
            {"id": "r2", "reply_text": "R2-REPLY"},
        ]),
        cooldown=make_cooldown(fake_clock),
    )
    scheduler = await _build([pool], max_retries=3)
    _force_first(pool)
    resp = await scheduler.chat_completion(make_chat_request())
    assert resp.text == "R2-REPLY"


async def test_unknown_model_raises_404(fake_clock):
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([{"id": "r1"}]),
        cooldown=make_cooldown(fake_clock),
    )
    scheduler = await _build([pool])
    with pytest.raises(ModelNotFoundError):
        await scheduler.chat_completion(make_chat_request(model="no-such-model"))


async def test_stream_success_yields_chunks(fake_clock):
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([
            {"id": "r1", "reply_text": "hello world"},
        ]),
        cooldown=make_cooldown(fake_clock),
    )
    scheduler = await _build([pool])
    chunks = [c async for c in scheduler.stream_chat(make_chat_request())]
    assert len(chunks) >= 2
    assert chunks[-1].finish_reason == "stop"
    assert "".join(c.text or "" for c in chunks) == "hello world "


async def test_stream_all_rate_limited_raises(fake_clock):
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([
            {"id": "r1", "scenario": "rate_limit", "retry_after": 1.0},
        ]),
        cooldown=make_cooldown(fake_clock),
    )
    scheduler = await _build([pool])
    with pytest.raises(RateLimitError):
        async for _ in scheduler.stream_chat(make_chat_request()):
            pass


async def test_list_models_aggregates(fake_clock):
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([{"id": "r1"}]),
        cooldown=make_cooldown(fake_clock),
    )
    scheduler = await _build([pool])
    models = await scheduler.list_models()
    assert len(models) >= 1
    model_ids = [m.id for m in models]
    assert "gemini-3.8-flash" in model_ids
