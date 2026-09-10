"""TASK-004 integration tests for the FirebaseProvider (no real network).

Full chains verified through the fake HTTP transport:
  OpenAI ChatRequest -> FirebaseProvider -> Gemini -> ChatResponse
  OpenAI stream=true -> FirebaseProvider -> Firebase SSE -> ChatChunk
Plus factory/registry wiring and scheduler cooldown on 429.
"""
from __future__ import annotations

import pytest

from core.cooldown import CooldownManager
from core.errors import RateLimitError
from core.health import HealthState
from core.models import ChatMessage, ChatRequest
from core.pool import InMemoryPool
from core.scheduler import Scheduler
from protocol.openai import to_openai_chat_response

from providers.firebase.factory import (
    FirebaseProviderFactory,
    FirebaseResourceFactory,
)
from providers.firebase.provider import FirebaseProvider, DEFAULT_MODELS

from tests.conftest import FakeClock
from tests.providers._firebase_fakes import (
    FakeHttp,
    FakeResponse,
    make_resource,
    make_sse,
)


def make_chat_request(**kwargs):
    return ChatRequest(
        model="gemini-3.8-flash",
        messages=[ChatMessage(role="user", content="hello")],
        **kwargs,
    )


def response_json(text="Hello from Firebase!"):
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 3,
            "candidatesTokenCount": 4,
            "thoughtsTokenCount": 0,
            "totalTokenCount": 7,
        },
    }


def sse_body():
    return make_sse(
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "Hello "}]}}]},
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "world"}]}}]},
        {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": ""}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"totalTokenCount": 7},
        },
        "[DONE]",
    )


async def test_provider_complete(fake_http):
    provider = FirebaseProvider(http_client=fake_http)
    resource = make_resource()
    fake_http.responses.append(fake_http.exchange_ok())
    fake_http.responses.append(FakeResponse(200, json_body=response_json()))

    resp = await provider.complete(make_chat_request(), resource)

    assert resp.text == "Hello from Firebase!"
    assert resp.finish_reason == "stop"
    assert resp.usage.total_tokens == 7

    # headers verified: api key + app id + app check applied to AI call
    ai_call = fake_http.post_calls[-1]
    assert ai_call["url"] == (
        "https://firebasevertexai.googleapis.com/v1beta/projects/test-project"
        "/models/gemini-3.8-flash:generateContent"
    )
    assert ai_call["headers"]["x-goog-api-key"] == "AIzaSyTESTAPIKEY"
    assert ai_call["headers"]["X-Firebase-Appid"] == "1:12345:web:abc123"
    assert ai_call["headers"]["X-Firebase-AppCheck"] == "jwt-token-1"
    assert ai_call["json"]["contents"][0]["parts"][0]["text"] == "hello"


async def test_provider_stream(fake_http):
    provider = FirebaseProvider(http_client=fake_http)
    resource = make_resource()
    fake_http.responses.append(fake_http.exchange_ok())
    fake_http.stream_responses.append(FakeResponse(200, sse_chunks=[sse_body()]))

    chunks = [c async for c in provider.stream(make_chat_request(), resource)]

    assert chunks[0].text == "Hello "
    assert chunks[1].text == "world"
    assert chunks[-1].finish_reason == "stop"

    ai_call = fake_http.stream_calls[-1]
    assert ":streamGenerateContent?alt=sse" in ai_call["url"]


async def test_provider_stream_401_refreshes_jwt(fake_http):
    provider = FirebaseProvider(http_client=fake_http)
    resource = make_resource()
    fake_http.responses.append(fake_http.exchange_ok(token="jwt-1"))
    fake_http.stream_responses.append(FakeResponse(401, content=b'{"error":{"message":"expired"}}'))
    fake_http.responses.append(fake_http.exchange_ok(token="jwt-2"))
    fake_http.stream_responses.append(FakeResponse(200, sse_chunks=[sse_body()]))

    chunks = [c async for c in provider.stream(make_chat_request(), resource)]

    assert chunks and chunks[0].text == "Hello "
    # second stream call carries the refreshed JWT
    assert fake_http.stream_calls[-1]["headers"]["X-Firebase-AppCheck"] == "jwt-2"


async def test_provider_list_models():
    provider = FirebaseProvider(models=["gemini-3.8-flash", "gemini-3.7-flash"])
    models = await provider.list_models()
    ids = [m.id for m in models]
    assert ids == ["gemini-3.8-flash", "gemini-3.7-flash"]
    assert all(m.provider == "firebase" for m in models)


async def test_provider_default_models():
    provider = FirebaseProvider()
    ids = [m.id for m in await provider.list_models()]
    assert ids == DEFAULT_MODELS


async def test_provider_health_check():
    provider = FirebaseProvider()
    ok = make_resource()
    assert (await provider.health_check(ok)).state == HealthState.HEALTHY

    bad = make_resource(id="down")
    bad.health = HealthState.COOLDOWN
    assert (await provider.health_check(bad)).state == HealthState.COOLDOWN


def test_factory_wiring():
    factory = FirebaseProviderFactory()
    provider = factory.create_provider("firebase", {"models": ["gemini-3.8-flash"]})
    assert isinstance(provider, FirebaseProvider)

    rf = FirebaseResourceFactory()
    resources = rf.create_resources(
        "firebase",
        [
            {
                "id": "project-a",
                "project_id": "p-a",
                "api_key": "k-a",
                "app_id": "a-a",
                "debug_token": "d-a",
            },
            {
                "id": "project-b",
                "project_id": "p-b",
                "api_key": "k-b",
                "app_id": "a-b",
                "debug_token": "d-b",
                "proxy": "socks5://user:pass@host:1080",
            },
        ],
    )
    assert len(resources) == 2
    assert resources[0].project_id == "p-a"
    assert resources[1].proxy == "socks5://user:pass@host:1080"
    assert resources[1].provider == "firebase"


def test_non_streaming_tool_call_serializes_to_openai():
    from providers.firebase.response import parse_response

    data = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"functionCall": {"name": "add", "args": {"a": 1, "b": 2}}}],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {},
    }
    resp = parse_response(data, "gemini-3.8-flash")
    openai = to_openai_chat_response(resp)
    assert openai["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "add"


async def test_scheduler_cooldown_on_429(fake_http, fake_clock):
    """429 -> RateLimitError(scope=resource) -> core cooldown honours Retry-After."""
    resource = make_resource()
    provider = FirebaseProvider(http_client=fake_http)

    cooldown = CooldownManager(now_fn=lambda: fake_clock.now)
    pool = InMemoryPool(provider="firebase", resources=[resource], cooldown=cooldown)
    scheduler = Scheduler(
        providers={"firebase": provider},
        pools={"firebase": pool},
        max_retries=1,
    )

    fake_http.responses.append(fake_http.exchange_ok())
    fake_http.responses.append(
        FakeResponse(
            429,
            headers={"Retry-After": "5"},
            content=b'{"error": {"message": "quota exceeded"}}',
        )
    )

    with pytest.raises(RateLimitError) as ei:
        await scheduler.chat_completion(make_chat_request())

    assert ei.value.scope == "resource"
    assert ei.value.retry_after == 5.0
    assert resource.health == HealthState.COOLDOWN
    assert resource.cooldown_until is not None

    # Retry-After is honoured with jitter (0-10%), so cooldown_until
    # should be between now+5s and now+5.5s
    delta = resource.cooldown_until - fake_clock.now
    assert 5.0 <= delta.total_seconds() <= 5.5


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_http():
    return FakeHttp()


@pytest.fixture
def fake_clock():
    return FakeClock()
