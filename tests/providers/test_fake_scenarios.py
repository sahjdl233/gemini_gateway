"""FakeProvider extended error scenarios (TASK-001 task 8)."""

from __future__ import annotations

import pytest

from core.errors import (
    AuthenticationError,
    AuthorizationError,
    ContentFilterError,
    ModelNotFoundError,
    RateLimitError,
    TimeoutError,
    UpstreamUnavailableError,
)
from providers.fake import FakeProvider

from tests.conftest import make_chat_request, make_resources


async def test_401_auth_error():
    [res] = make_resources([{"id": "r1", "scenario": "auth_error"}])
    with pytest.raises(AuthenticationError):
        await FakeProvider().complete(make_chat_request(), res)


async def test_403_authorization_error():
    [res] = make_resources([{"id": "r1", "scenario": "authz_error"}])
    with pytest.raises(AuthorizationError):
        await FakeProvider().complete(make_chat_request(), res)


async def test_500_server_error():
    [res] = make_resources([{"id": "r1", "scenario": "server_error"}])
    with pytest.raises(UpstreamUnavailableError):
        await FakeProvider().complete(make_chat_request(), res)


async def test_404_model_not_found():
    [res] = make_resources([{"id": "r1", "scenario": "unknown_model"}])
    with pytest.raises(ModelNotFoundError):
        await FakeProvider().complete(make_chat_request(), res)


async def test_content_filter():
    [res] = make_resources([{"id": "r1", "scenario": "content_filter"}])
    with pytest.raises(ContentFilterError):
        await FakeProvider().complete(make_chat_request(), res)


async def test_stream_error_raises_before_chunks():
    [res] = make_resources([{"id": "r1", "scenario": "authz_error"}])
    with pytest.raises(AuthorizationError):
        async for _ in FakeProvider().stream(make_chat_request(), res):
            pass


async def test_stream_multiple_chunks_finish_reason():
    [res] = make_resources([{"id": "r1", "reply_text": "alpha beta gamma"}])
    chunks = [c async for c in FakeProvider().stream(make_chat_request(), res)]
    assert [c.text for c in chunks[:3]] == ["alpha ", "beta ", "gamma "]
    assert chunks[-1].finish_reason == "stop"


async def test_stream_timeout():
    [res] = make_resources([{"id": "r1", "scenario": "timeout"}])
    with pytest.raises(TimeoutError):
        async for _ in FakeProvider().stream(make_chat_request(), res):
            pass
