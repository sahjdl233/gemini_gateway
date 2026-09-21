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
from core.models import ChatChunk
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
    pool._cursor = 0


class LifecyclePool(InMemoryPool):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.releases = []
        self.acquires = []

    async def acquire(self, *, skip=None):
        resource = await super().acquire(skip=skip)
        if resource is not None:
            self.acquires.append(resource.id)
        return resource

    async def release(self, resource):
        self.releases.append(resource.id)
        return await super().release(resource)


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


class PartialStreamProvider(CountingProvider):
    async def stream(self, request, resource):
        self.calls.append(resource.id)
        yield ChatChunk(id="chatcmpl-fake-r1", model="fake-1", text="R1-STREAM")
        self.stream_started = True
        raise TimeoutError("partial")


async def test_stream_resource_lifecycle_success(fake_clock):
    provider = CountingProvider()
    pool = LifecyclePool(
        provider="fake",
        resources=make_resources([{"id": "r1", "reply_text": "R1"}]),
        cooldown=make_cooldown(fake_clock),
    )
    scheduler = Scheduler(providers={"fake": provider}, pools={"fake": pool})

    chunks = [chunk async for chunk in scheduler.stream_chat(make_chat_request())]

    assert chunks[0].text == "R1 "
    assert chunks[-1].finish_reason == "stop"
    assert len(chunks) == 2
    assert provider.calls == ["r1"]
    assert pool.acquires == ["r1"]
    assert pool.releases == ["r1"]
    assert pool.resources[0].in_flight == 0


async def test_stream_resource_lifecycle_failure(fake_clock):
    provider = PartialStreamProvider()
    pool = LifecyclePool(
        provider="fake",
        resources=make_resources([
            {"id": "r1", "reply_text": "R1"},
            {"id": "r2", "reply_text": "R2"},
        ]),
        cooldown=make_cooldown(fake_clock),
    )
    scheduler = Scheduler(
        providers={"fake": provider}, pools={"fake": pool}, max_retries=0
    )

    async def drain():
        async for _ in scheduler.stream_chat(make_chat_request()):
            pass

    with pytest.raises(TimeoutError):
        await drain()

    # The stream had already started before the provider failure; the
    # scheduler must still return the acquired resource to the pool.
    assert provider.stream_started is True
    assert provider.calls[0] == "r1"
    assert pool.acquires[0] == "r1"
    assert pool.releases[0] == "r1"
    assert pool.resources[0].in_flight == 0

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
    assert pool.resources[0].total_requests == 1
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

    assert chunks[0].text.strip() == "R3-STREAM"
    assert provider.calls == ["r2", "r3"]


async def test_retryable_resource_failure_uses_load_aware_pool_selection(fake_clock):
    provider = CountingProvider()
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([
            {"id": "r1", "scenario": "timeout"},
            {"id": "r2", "reply_text": "R2-REPLY"},
        ]),
        cooldown=make_cooldown(fake_clock),
    )
    # Lower load selects r1 first; its timeout must fall back to r2.
    pool.resources[0].in_flight = 0
    pool.resources[1].in_flight = 1
    scheduler = Scheduler(
        providers={"fake": provider},
        pools={"fake": pool},
        max_retries=2,
    )

    response = await scheduler.chat_completion(make_chat_request())

    assert response.text == "R2-REPLY"
    assert provider.calls == ["r1", "r2"]
    assert pool.resources[0].total_failures == 1
    assert pool.resources[0].total_requests == 1
    assert pool.resources[1].total_failures == 0
    assert pool.resources[1].total_requests == 1


async def test_success_records_before_releases(fake_clock):
    """Outcome must be recorded before release for successful completions."""
    provider = CountingProvider()
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([{"id": "r1", "reply_text": "R1-REPLY"}]),
        cooldown=make_cooldown(fake_clock),
    )
    _force_first(pool)
    scheduler = Scheduler(
        providers={"fake": provider},
        pools={"fake": pool},
        max_retries=2,
    )
    response = await scheduler.chat_completion(make_chat_request())
    assert response.text == "R1-REPLY"
    assert pool.resources[0].total_requests == 1
    assert pool.resources[0].total_failures == 0


async def test_failure_records_before_releases(fake_clock):
    """ProviderError outcome must be recorded before release."""
    provider = CountingProvider()
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([{"id": "r1", "scenario": "timeout"}]),
        cooldown=make_cooldown(fake_clock),
    )
    _force_first(pool)
    scheduler = Scheduler(
        providers={"fake": provider},
        pools={"fake": pool},
        max_retries=2,
    )
    with pytest.raises(TimeoutError):
        await scheduler.chat_completion(make_chat_request())
    assert pool.resources[0].total_failures == 1
    assert pool.resources[0].total_requests == 1


async def test_rate_limit_sets_cooldown_before_release(fake_clock):
    """429 must put resource in COOLDOWN before release."""
    provider = CountingProvider()
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([
            {"id": "r1", "scenario": "rate_limit", "retry_after": 5.0},
            {"id": "r2", "reply_text": "R2-REPLY"},
        ]),
        cooldown=make_cooldown(fake_clock),
    )
    _force_first(pool)
    scheduler = Scheduler(
        providers={"fake": provider},
        pools={"fake": pool},
        max_retries=2,
    )
    response = await scheduler.chat_completion(make_chat_request())
    assert response.text == "R2-REPLY"
    r1 = pool.resources[0]
    assert r1.health.name == "COOLDOWN"
    assert r1.cooldown_until is not None


async def test_stream_records_failure_before_release_then_falls_back(fake_clock):
    """Stream retryable error before first chunk: record, release, fallback."""
    provider = CountingProvider()
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([
            {"id": "r1", "scenario": "timeout"},
            {"id": "r2", "reply_text": "R2-STREAM"},
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
    assert chunks[0].text.strip() == "R2-STREAM"
    assert provider.calls == ["r1", "r2"]
    assert pool.resources[0].total_failures == 1
    assert pool.resources[0].total_requests == 1


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

async def test_stream_partial_failure(fake_clock):
    provider = PartialStreamProvider()
    pool = InMemoryPool(
        provider="fake",
        resources=make_resources([
            {"id": "r1", "reply_text": "R1-STREAM"},
            {"id": "r2", "reply_text": "R2-STREAM"},
        ]),
        cooldown=make_cooldown(fake_clock),
    )
    _force_first(pool)
    scheduler = Scheduler(
        providers={"fake": provider},
        pools={"fake": pool},
        max_retries=2,
    )

    chunks = []
    print(f"[DEBUG-TEST] resources: {[r.id for r in pool.resources]}")
    print(f"[DEBUG-TEST] cursor: {pool._cursor}")
    with pytest.raises(TimeoutError):
        async for chunk in scheduler.stream_chat(make_chat_request()):
            chunks.append(chunk)

    assert [c.text for c in chunks] == ["R1-STREAM"]
    print(f"[DEBUG-TEST] provider calls: {provider.calls}")
    assert provider.calls == ["r1"]
    assert pool.resources[0].total_failures == 1
    assert pool.resources[1].total_requests == 0
