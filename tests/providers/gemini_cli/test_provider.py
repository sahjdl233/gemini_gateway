"""Non-stream / stream provider unit coverage + provider wiring (TASK-008)."""
from __future__ import annotations

from typing import Any, List

import pytest

from providers.gemini_cli.client import DEFAULT_BASE_URL
from providers.gemini_cli.provider import GeminiCliProvider
from providers.gemini_cli.resource import GeminiCliResource
from tests.providers._gemini_cli_fakes import (
    FakeHttp,
    FakeResponse,
    envelope,
    make_backend,
    make_resource,
)
from core.models import ChatMessage, ChatRequest

# Fix FakeResponse import visibility
FakeResponse  # noqa


async def test_list_models():
    provider = GeminiCliProvider(models=["gemini-2.5-flash", "gemini-2.5-pro"])
    models = await provider.list_models()
    assert [m.id for m in models] == ["gemini-2.5-flash", "gemini-2.5-pro"]


async def test_complete_uses_project_from_resource():
    http = FakeHttp()
    http.responses.append(http.token_ok("token-1", 3600))
    http.responses.append(
        http.ok(
            {
                "response": {
                    "candidates": [{"content": {"role": "model", "parts": [{"text": "resp"}]}, "finishReason": "STOP"}]
                },
                "traceId": "trace-1",
            }
        )
    )
    provider = GeminiCliProvider()
    provider.set_http_client(http)
    resource = make_resource(project_id="gen-lang-client-test")

    req = ChatRequest(model="gemini-2.5-flash", messages=[ChatMessage(role="user", content="q")])
    resp = await provider.complete(req, resource)

    assert resp.text == "resp"
    # Verify the envelope was sent with project
    call = http.post_calls[-1]
    assert call["json"]["project"] == "gen-lang-client-test"
    assert call["json"]["model"] == "gemini-2.5-flash"
    # URL is the v1internal generateContent endpoint
    assert "generateContent" in call["url"]
    assert "streamGenerateContent" not in call["url"]


async def test_complete_streaming_url_has_alt_sse():
    http = FakeHttp()
    http.responses.append(http.token_ok("token-1", 3600))
    http.stream_responses.append(
        FakeResponse(
            200,
            sse_chunks=[
                (b'data: {"response":{"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}}\n\n')
            ],
        )
    )
    provider = GeminiCliProvider()
    provider.set_http_client(http)
    resource = make_resource()

    req = ChatRequest(model="gemini-2.5-flash", messages=[ChatMessage(role="user", content="q")], stream=True)
    chunks = [chunk async for chunk in provider.stream(req, resource)]
    assert chunks and chunks[0].text == "hi"
    call = http.stream_calls[-1]
    assert "streamGenerateContent" in call["url"]
    assert "alt=sse" in call["url"]


async def test_complete_missing_project_raises():
    provider = GeminiCliProvider()
    provider.set_http_client(FakeHttp())
    resource = make_resource(project_id=None)
    req = ChatRequest(model="gemini-2.5-flash", messages=[ChatMessage(role="user", content="q")])
    from providers.gemini_cli.errors import GeminiCliProtocolError
    with pytest.raises(GeminiCliProtocolError):
        await provider.complete(req, resource)


def test_provider_http_client_injection():
    provider = GeminiCliProvider()
    fake = FakeHttp()
    provider.set_http_client(fake)
    # TASK-ARCH-004: injection now lands on the shared ExecutionBackend,
    # which borrows the client (owned=False) instead of the provider
    # holding a bare per-resource HTTP path.
    from execution.http import HttpExecutionBackend

    assert isinstance(provider.backend, HttpExecutionBackend)
    assert provider.backend._client is fake
    assert provider.backend._owns_client is False



# ---------------------------------------------------------------------------
# TASK-009: provider-level integration tests (complete & stream)
# ---------------------------------------------------------------------------


class _AuthFakeClock:
    def __init__(self, start=1000.0):
        self.value = start

    def time(self):
        return self.value


def _provider_with_http(http):
    """Create a GeminiCliProvider with injected FakeHttp and token auth."""
    from providers.gemini_cli.auth import GeminiCliAuth
    from providers.gemini_cli.client import GeminiCliClient

    auth = GeminiCliAuth(http, clock=_AuthFakeClock())
    client = GeminiCliClient(backend=make_backend(http), auth=auth)
    provider = GeminiCliProvider(models=["gemini-2.5-flash"])
    # monkeypatch internal client cache
    provider._clients.clear()
    provider.set_http_client(http)
    # we need to intercept _client_for to use our pre-authenticated client
    original_client_for = provider._client_for
    async def _mock_client_for(resource):
        return client
    provider._client_for = _mock_client_for
    return provider


async def test_complete_existing_project_no_onboarding():
    """Resource with project_id skips loadCodeAssist/onboard entirely."""
    http = FakeHttp()
    # token + generateContent
    http.responses.append(http.token_ok("token-1", 3600))
    http.responses.append(
        http.ok(
            {
                "response": {
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": "hello"}]},
                            "finishReason": "STOP",
                        }
                    ]
                },
                "traceId": "t1",
            }
        )
    )
    provider = _provider_with_http(http)
    resource = make_resource(project_id="gen-lang-client-123")

    req = ChatRequest(model="gemini-2.5-flash", messages=[ChatMessage(role="user", content="hi")])
    resp = await provider.complete(req, resource)

    assert resp.text == "hello"
    # exactly 2 calls: token refresh + generateContent
    assert len(http.post_calls) == 2
    assert "generateContent" in http.post_calls[-1]["url"]
    assert "loadCodeAssist" not in str(http.post_calls)
    assert "onboardUser" not in str(http.post_calls)


async def test_complete_missing_project_triggers_discovery_writeback():
    """No project -> onboarding -> project written back to resource."""
    http = FakeHttp()
    # loadCodeAssist: token + response
    http.responses.append(http.token_ok("token-1", 3600))
    http.responses.append(http.ok({"allowedTiers": [{"id": "g1-pro-tier", "isDefault": True}]}))
    # onboardUser: cached token + response
    http.responses.append(http.ok({"name": "operations/op-1"}))
    # poll: cached token + response
    http.responses.append(
        http.ok(
            {"done": True, "response": {"cloudaicompanionProject": {"id": "proj-after-lro"}}}
        )
    )
    # generateContent: cached token + response
    http.responses.append(
        http.ok(
            {
                "response": {
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": "ok"}]},
                            "finishReason": "STOP",
                        }
                    ]
                },
                "traceId": "t1",
            }
        )
    )
    provider = _provider_with_http(http)
    resource = make_resource(project_id=None)

    req = ChatRequest(model="gemini-2.5-flash", messages=[ChatMessage(role="user", content="hi")])
    resp = await provider.complete(req, resource)

    assert resp.text == "ok"
    assert resource.project_id == "proj-after-lro"
    # Verify onboarding happened
    urls = [c["url"] for c in http.post_calls]
    assert any("loadCodeAssist" in u for u in urls)
    assert any("onboardUser" in u for u in urls)


async def test_complete_second_request_uses_cached_project():
    """Second complete() on same resource reuses project_id, no re-onboarding."""
    http = FakeHttp()
    # First request: onboarding + generate
    http.responses.append(http.token_ok("token-1", 3600))
    http.responses.append(http.ok({"allowedTiers": [{"id": "g1-pro-tier", "isDefault": True}]}))
    http.responses.append(http.ok({"name": "operations/op-1"}))
    http.responses.append(
        http.ok(
            {"done": True, "response": {"cloudaicompanionProject": {"id": "proj-after-lro"}}}
        )
    )
    http.responses.append(
        http.ok(
            {
                "response": {
                    "candidates": [
                        {"content": {"role": "model", "parts": [{"text": "first"}]}, "finishReason": "STOP"}
                    ]
                },
                "traceId": "t1",
            }
        )
    )
    # Second request: only generate (no onboarding calls)
    http.responses.append(
        http.ok(
            {
                "response": {
                    "candidates": [
                        {"content": {"role": "model", "parts": [{"text": "second"}]}, "finishReason": "STOP"}
                    ]
                },
                "traceId": "t2",
            }
        )
    )
    provider = _provider_with_http(http)
    resource = make_resource(project_id=None)

    req = ChatRequest(model="gemini-2.5-flash", messages=[ChatMessage(role="user", content="hi")])
    resp1 = await provider.complete(req, resource)
    resp2 = await provider.complete(req, resource)

    assert resp1.text == "first"
    assert resp2.text == "second"
    assert resource.project_id == "proj-after-lro"
    # No loadCodeAssist/onboard on second request
    urls = [c["url"] for c in http.post_calls]
    load_calls = [u for u in urls if "loadCodeAssist" in u]
    onboard_calls = [u for u in urls if "onboardUser" in u]
    assert len(load_calls) == 1
    assert len(onboard_calls) == 1
    # 2nd generateContent used cached project
    gen_calls = [u for u in urls if "generateContent" in u]
    assert len(gen_calls) == 2
