"""Non-stream / stream provider unit coverage + provider wiring (TASK-008)."""
from __future__ import annotations

from typing import Any, List

import pytest

from core.models import ChatMessage, ChatRequest
from providers.gemini_cli.client import DEFAULT_BASE_URL
from providers.gemini_cli.provider import GeminiCliProvider
from providers.gemini_cli.resource import GeminiCliResource
from tests.providers._gemini_cli_fakes import FakeHttp, FakeResponse, envelope, make_resource

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
    assert provider._http is fake
