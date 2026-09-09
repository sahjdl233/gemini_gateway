"""FakeProvider scenario tests (TASK-000 rule 26)."""

from __future__ import annotations

import pytest

from core.errors import (
    AuthenticationError,
    ModelNotFoundError,
    RateLimitError,
    TimeoutError,
)
from core.health import HealthState
from providers.fake import FakeProvider

from tests.conftest import make_chat_request, make_resources


async def test_success_complete():
    [res] = make_resources([{"id": "r1", "reply_text": "R1-REPLY"}])
    provider = FakeProvider()
    resp = await provider.complete(make_chat_request(), res)
    assert resp.text == "R1-REPLY"
    assert resp.finish_reason == "stop"
    assert resp.usage.total_tokens == 2


async def test_rate_limit_complete():
    [res] = make_resources([{"id": "r1", "scenario": "rate_limit", "retry_after": 2.0}])
    provider = FakeProvider()
    with pytest.raises(RateLimitError) as ei:
        await provider.complete(make_chat_request(), res)
    assert ei.value.retry_after == 2.0
    assert ei.value.scope == "resource"
    assert ei.value.resource_id == "r1"


async def test_auth_error_complete():
    [res] = make_resources([{"id": "r1", "scenario": "auth_error"}])
    provider = FakeProvider()
    with pytest.raises(AuthenticationError):
        await provider.complete(make_chat_request(), res)


async def test_timeout_complete():
    [res] = make_resources([{"id": "r1", "scenario": "timeout"}])
    provider = FakeProvider()
    with pytest.raises(TimeoutError):
        await provider.complete(make_chat_request(), res)


async def test_unknown_model_complete():
    [res] = make_resources([{"id": "r1", "scenario": "unknown_model"}])
    provider = FakeProvider()
    with pytest.raises(ModelNotFoundError):
        await provider.complete(make_chat_request(model="ghost"), res)


async def test_stream_success():
    [res] = make_resources([{"id": "r1", "reply_text": "two words"}])
    provider = FakeProvider()
    chunks = [c async for c in provider.stream(make_chat_request(), res)]
    assert chunks[0].text == "two "
    assert chunks[1].text == "words "
    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].usage.completion_tokens == 2


async def test_stream_rate_limit():
    [res] = make_resources([{"id": "r1", "scenario": "rate_limit"}])
    provider = FakeProvider()
    with pytest.raises(RateLimitError):
        async for _ in provider.stream(make_chat_request(), res):
            pass


async def test_health_check_scenarios():
    provider = FakeProvider()
    [ok] = make_resources([{"id": "r1"}])
    [bad] = make_resources([{"id": "r2", "scenario": "rate_limit"}])
    assert (await provider.health_check(ok)).state == HealthState.HEALTHY
    assert (await provider.health_check(bad)).state == HealthState.DEGRADED


async def test_no_real_network_used():
    """FakeProvider must never touch the network (TASK-000 rule)."""
    from app.main import build_runtime
    from config.loader import default_config

    scheduler = build_runtime(default_config())
    assert set(scheduler.providers.keys()) == {"fake"}
