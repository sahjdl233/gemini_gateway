"""TASK-002 tests for the Anonymous Vertex provider (no real network)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.errors import (
    AuthenticationError,
    InvalidRequestError,
    ModelNotFoundError,
    ProviderError,
    RateLimitError,
    UpstreamUnavailableError,
)
from core.health import HealthState
from core.models import ChatChunk, ChatMessage, ChatRequest, ChatResponse

from providers.anonymous_vertex.provider import AnonymousVertexProvider
from providers.anonymous_vertex.resource import AnonymousVertexResource
from providers.anonymous_vertex.protocol import (
    chat_to_vertex_request,
    get_model_spec,
    vertex_response_to_chat_response,
)
from providers.anonymous_vertex.signature import (
    OPERATION_NAME,
    QUERY_SIGNATURE,
    apply_thought_signature,
    ensure_base64_sig,
    trim_gemini_path_prefix,
)
from providers.anonymous_vertex.request import build_envelope
from providers.anonymous_vertex.streaming import (
    StreamParseError,
    StreamingObjectScanner,
    chunk_finish_reason,
    extract_chunk_from_frame,
    iter_chunks,
    normalize_chunk,
)
from providers.anonymous_vertex.errors import (
    classify_upstream_error,
    parse_upstream_error,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "anonymous_vertex"


def make_resource():
    return AnonymousVertexResource(id="default", provider="anonymous_vertex")


def make_request(model="gemini-2.5-flash", content="hello"):
    return ChatRequest(
        model=model,
        messages=[ChatMessage(role="user", content=content)],
    )


# ---------------------------------------------------------------------------
# Test 1: Request Conversion
# ---------------------------------------------------------------------------
def test_request_conversion_basic():
    req = make_request()
    vreq = chat_to_vertex_request(req)
    assert vreq["contents"][0]["role"] == "user"
    assert vreq["contents"][0]["parts"][0]["text"] == "hello"
    assert vreq["safetySettings"][0]["threshold"] == "BLOCK_NONE"
    assert vreq["generationConfig"]["maxOutputTokens"] == 65535


def test_request_conversion_system_message():
    req = ChatRequest(
        model="gemini-2.5-flash",
        messages=[
            ChatMessage(role="system", content="You are helpful"),
            ChatMessage(role="user", content="hi"),
        ],
    )
    vreq = chat_to_vertex_request(req)
    assert vreq["systemInstruction"]["parts"][0]["text"] == "You are helpful"
    assert vreq["contents"][0]["role"] == "user"


def test_request_conversion_assistant_to_model():
    req = ChatRequest(
        model="gemini-2.5-flash",
        messages=[
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="hello there"),
        ],
    )
    vreq = chat_to_vertex_request(req)
    assert vreq["contents"][1]["role"] == "model"
    assert vreq["contents"][1]["parts"][0]["text"] == "hello there"


def test_request_conversion_merge_contiguous_user():
    req = ChatRequest(
        model="gemini-2.5-flash",
        messages=[
            ChatMessage(role="user", content="a"),
            ChatMessage(role="user", content="b"),
        ],
    )
    vreq = chat_to_vertex_request(req)
    # merged into one content entry
    assert len(vreq["contents"]) == 1
    parts_text = "".join(p["text"] for p in vreq["contents"][0]["parts"])
    assert parts_text == "ab"


def test_request_conversion_temperature():
    req = make_request()
    req.temperature = 0.7
    vreq = chat_to_vertex_request(req)
    assert vreq["generationConfig"]["temperature"] == 0.7


def test_request_conversion_max_tokens_clamped():
    req = make_request()
    req.max_tokens = 100000  # above spec max
    vreq = chat_to_vertex_request(req)
    assert vreq["generationConfig"]["maxOutputTokens"] == 65535


# ---------------------------------------------------------------------------
# Test 2: Signature
# ---------------------------------------------------------------------------
def test_query_signature_is_fixed_constant():
    # source: payload.go
    assert QUERY_SIGNATURE == "2/l8eCsMMY49imcDQ/lwwXyL8cYtTjxZBF2dNqy69LodY="
    assert OPERATION_NAME == "StreamGenerateContentAnonymous"


def test_thought_signature_sentinel():
    import base64
    sig = apply_thought_signature("x", is_thought=True)
    expected = base64.b64encode(b"skip_thought_signature_validator").decode()
    assert sig == expected


def test_ensure_base64_sig_preserves_valid():
    import base64
    sig = base64.b64encode(b"abc").decode()
    assert ensure_base64_sig(sig) == sig


def test_ensure_base64_sig_encodes_sentinel():
    from providers.anonymous_vertex.signature import SKIP_THOUGHT_SENTINEL
    assert ensure_base64_sig(SKIP_THOUGHT_SENTINEL) == "c2tpcF90aG91Z2h0X3NpZ25hdHVyZV92YWxpZGF0b3I="


def test_trim_gemini_path_prefix():
    assert trim_gemini_path_prefix("models/gemini-2.5-flash") == "gemini-2.5-flash"
    assert trim_gemini_path_prefix("gemini-2.5-flash") == "gemini-2.5-flash"


def test_build_envelope_includes_query_signature():
    env = build_envelope("gemini-2.5-flash", {}, "recaptcha-token")
    assert env["querySignature"] == QUERY_SIGNATURE
    assert env["operationName"] == OPERATION_NAME
    assert env["variables"]["region"] == "global"
    assert env["variables"]["recaptchaToken"] == "recaptcha-token"


# ---------------------------------------------------------------------------
# Test 3: Response Conversion
# ---------------------------------------------------------------------------
def test_response_conversion():
    candidates = [
        {
            "index": 0,
            "content": {"role": "model", "parts": [{"text": "Hello world"}]},
            "finishReason": "STOP",
        }
    ]
    usage = {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}
    resp = vertex_response_to_chat_response(
        candidates, usage_metadata=usage, model_version="gemini-2.5-flash"
    )
    assert resp.text == "Hello world"
    assert resp.finish_reason == "stop"
    assert resp.usage.total_tokens == 15


def test_response_conversion_unspecified_finish():
    candidates = [
        {"index": 0, "content": {"role": "model", "parts": [{"text": "hi"}]}, "finishReason": "FINISH_REASON_UNSPECIFIED"}
    ]
    resp = vertex_response_to_chat_response(candidates)
    assert resp.text == "hi"
    assert resp.finish_reason == "stop"


# ---------------------------------------------------------------------------
# Test 4: Streaming
# ---------------------------------------------------------------------------
def test_streaming_scanner_single_object():
    scanner = StreamingObjectScanner()
    raw = b'{"a":1}'
    objs = scanner.feed(raw)
    assert len(objs) == 1
    assert objs[0] == b'{"a":1}'


def test_streaming_scanner_split_chunks():
    scanner = StreamingObjectScanner()
    raw = b'{"a":1}{"b":2}'
    # feed in two pieces
    objs1 = scanner.feed(raw[:5])
    objs2 = scanner.feed(raw[5:])
    all_objs = objs1 + objs2
    assert len(all_objs) == 2
    assert all_objs[0] == b'{"a":1}'


def test_streaming_scanner_nested():
    scanner = StreamingObjectScanner()
    raw = b'{"results":[{"a":{"b":2}}]}'
    objs = scanner.feed(raw)
    assert len(objs) == 1


def test_extract_chunk_from_frame():
    with open(FIXTURES / "response.json", encoding="utf-8") as f:
        frame = f.read().encode()
    chunk = extract_chunk_from_frame(frame)
    assert chunk is not None
    assert chunk["candidates"][0]["content"]["parts"][0]["text"] == "Hello from Anonymous Vertex!"
    assert chunk_finish_reason(chunk) == "STOP"


def test_iter_chunks_yields_chunks():
    with open(FIXTURES / "stream.txt", encoding="utf-8") as f:
        lines = f.read().splitlines()

    async def gen():
        for line in lines:
            yield line.encode()

    import asyncio

    async def run():
        chunks = []
        async for chunk in iter_chunks(gen()):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(run())
    # 3 frames, but the last (STOP with usage) normalizes too
    assert len(chunks) >= 2
    # verify text content across chunks
    texts = []
    for c in chunks:
        n = normalize_chunk(c)
        if isinstance(n, list):
            for item in n:
                if item.get("candidates"):
                    for cand in item["candidates"]:
                        for p in (cand.get("content") or {}).get("parts", []):
                            texts.append(p.get("text", ""))
        elif n and n.get("candidates"):
            for cand in n["candidates"]:
                for p in (cand.get("content") or {}).get("parts", []):
                    texts.append(p.get("text", ""))
    assert "Hello " in texts
    assert "world!" in texts


def test_chunk_finish_reason_ignores_unspecified():
    chunk = {"candidates": [{"finishReason": "FINISH_REASON_UNSPECIFIED"}]}
    assert chunk_finish_reason(chunk) is None
    chunk2 = {"candidates": [{"finishReason": "STOP"}]}
    assert chunk_finish_reason(chunk2) == "STOP"


# ---------------------------------------------------------------------------
# Test 5 & 6: 429 and Retry-After
# ---------------------------------------------------------------------------
def test_parse_429_ratelimit():
    with open(FIXTURES / "error_429.json", encoding="utf-8") as f:
        body = f.read().encode()
    err = parse_upstream_error(429, body)
    assert err.kind == "ratelimit"
    assert err.status_code == 429
    mapped = classify_upstream_error(err)
    assert isinstance(mapped, RateLimitError)


def test_parse_400_invalid():
    with open(FIXTURES / "error_400.json", encoding="utf-8") as f:
        body = f.read().encode()
    err = parse_upstream_error(400, body)
    assert err.kind == "invalid"
    mapped = classify_upstream_error(err)
    assert isinstance(mapped, InvalidRequestError)


def test_429_with_retry_after_header():
    # Simulate a 429 response; provider reads Retry-After and passes to RateLimitError
    from providers.anonymous_vertex.errors import UpstreamVertexError
    err = UpstreamVertexError("rate limited", status_code=429, retry_after=10.0)
    mapped = classify_upstream_error(err)
    assert isinstance(mapped, RateLimitError)
    assert mapped.retry_after == 10.0


def test_provider_extracts_retry_after_header():
    """HTTP 429 with Retry-After exposes retry_after to Core Runtime (TASK-002 s13)."""
    from providers.anonymous_vertex.provider import AnonymousVertexProvider

    class Hdr(dict):
        pass

    class MockResp:
        def __init__(self, status_code, body, headers=None):
            self.status_code = status_code
            self.content = body
            self.headers = headers or {}

        async def aiter_bytes(self):
            yield b""

    body = b'{"error":{"code":429,"message":"RESOURCE_EXHAUSTED: rate limit hit","status":"RESOURCE_EXHAUSTED"}}'
    client = MockClient(
        responses={
            "post": MockResp(
                429,
                body,
                headers={"Retry-After": "7"},
            )
        }
    )
    provider = AnonymousVertexProvider(http_client=client, token_fetcher=_token_fetcher)

    import asyncio

    with pytest.raises(RateLimitError) as ei:
        asyncio.run(provider.complete(make_request(), make_resource()))
    assert ei.value.retry_after == 7.0


def test_provider_extract_retry_after_missing_header():
    """429 without Retry-After -> retry_after None (fallback to backoff)."""
    from providers.anonymous_vertex.provider import AnonymousVertexProvider

    class MockResp:
        def __init__(self, status_code=429, body=b"{}", headers=None):
            self.status_code = status_code
            self.content = body
            self.headers = headers or {}

        async def aiter_bytes(self):
            yield b""

    client = MockClient(responses={"post": MockResp(429, b"{}")})
    provider = AnonymousVertexProvider(http_client=client, token_fetcher=_token_fetcher)

    import asyncio

    with pytest.raises(RateLimitError) as ei:
        asyncio.run(provider.complete(make_request(), make_resource()))
    assert ei.value.retry_after is None


def test_stream_extracts_retry_after_header():
    """Streaming path also carries Retry-After on RateLimitError."""
    from providers.anonymous_vertex.provider import AnonymousVertexProvider

    class MockResp:
        def __init__(self, status_code=429, body=b"", headers=None):
            self.status_code = status_code
            self.content = body
            self.headers = headers or {}

        async def aiter_bytes(self):
            yield b""

    client = MockClient(
        responses={
            "post": MockResp(
                429,
                b"{}",
                headers={"retry-after": "12"},
            )
        }
    )
    provider = AnonymousVertexProvider(http_client=client, token_fetcher=_token_fetcher)

    import asyncio

    async def run():
        async for _ in provider.stream(make_request(), make_resource()):
            pass

    with pytest.raises(RateLimitError) as ei:
        asyncio.run(run())
    assert ei.value.retry_after == 12.0


# ---------------------------------------------------------------------------
# Test 7: Malformed Stream
# ---------------------------------------------------------------------------
def test_malformed_stream_raises():
    with open(FIXTURES / "malformed.txt", encoding="utf-8") as f:
        data = f.read().encode()
    import asyncio

    async def gen():
        yield data

    async def run():
        chunks = []
        with pytest.raises(ProviderError):
            async for c in iter_chunks(gen()):
                chunks.append(c)
        return chunks

    asyncio.run(run())


def test_scanner_malformed_json_raises():
    # incomplete JSON at EOF -> StreamParseError
    import asyncio

    async def gen():
        yield b'{"results":[{"data":'

    async def run():
        with pytest.raises(ProviderError):
            async for _ in iter_chunks(gen()):
                pass

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Provider-level tests with mock transport
# ---------------------------------------------------------------------------
class MockResponse:
    def __init__(self, status_code=200, body=b"", stream_bytes=None):
        self.status_code = status_code
        self._body = body
        self.content = body
        self._stream = stream_bytes

    async def aiter_bytes(self):
        if self._stream is not None:
            for chunk in self._stream:
                yield chunk
        elif self._body:
            yield self._body


class MockClient:
    """httpx.AsyncClient-compatible mock."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    async def post(self, url, content=None, headers=None):
        self.calls.append({"url": url, "content": content, "headers": headers})
        # Return a response based on the fixture
        return self.responses.get("post", MockResponse(200, b""))


async def _token_fetcher(resource):
    return "recaptcha-token"


def test_provider_complete_mock():
    with open(FIXTURES / "response.json", encoding="utf-8") as f:
        body = f.read().encode()
    client = MockClient(responses={"post": MockResponse(200, body)})
    provider = AnonymousVertexProvider(http_client=client, token_fetcher=_token_fetcher)

    import asyncio
    resp = asyncio.run(provider.complete(make_request(), make_resource()))
    assert isinstance(resp, ChatResponse)
    assert "Hello from Anonymous Vertex!" in resp.text


def test_provider_stream_mock():
    with open(FIXTURES / "stream.txt", encoding="utf-8") as f:
        raw = f.read().encode()
    # split into byte chunks to simulate streaming
    stream = [raw[i : i + 20] for i in range(0, len(raw), 20)]
    client = MockClient(responses={"post": MockResponse(200, stream_bytes=stream)})
    provider = AnonymousVertexProvider(http_client=client, token_fetcher=_token_fetcher)

    import asyncio
    async def run():
        chunks = []
        async for c in provider.stream(make_request(), make_resource()):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(run())
    texts = [c.text for c in chunks if c.text]
    assert any("Hello" in t for t in texts)


def test_provider_429_mock():
    with open(FIXTURES / "error_429.json", encoding="utf-8") as f:
        body = f.read().encode()
    client = MockClient(responses={"post": MockResponse(429, body)})
    provider = AnonymousVertexProvider(http_client=client, token_fetcher=_token_fetcher)

    import asyncio
    with pytest.raises(RateLimitError):
        asyncio.run(provider.complete(make_request(), make_resource()))


def test_provider_list_models():
    provider = AnonymousVertexProvider()
    import asyncio
    models = asyncio.run(provider.list_models())
    assert any(m.id == "gemini-2.5-flash" for m in models)


def test_provider_health_check():
    provider = AnonymousVertexProvider()
    import asyncio
    res = make_resource()
    result = asyncio.run(provider.health_check(res))
    assert result.state == HealthState.HEALTHY
