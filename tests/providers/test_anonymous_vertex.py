"""TASK-002 tests for the Anonymous Vertex provider (no real network)."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
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
from providers.anonymous_vertex.request import (
    chat_to_vertex_request,
    get_model_spec,
)
from providers.anonymous_vertex.response import vertex_response_to_chat_response
from providers.anonymous_vertex.signature import (
    OPERATION_NAME,
    QUERY_SIGNATURE,
    apply_thought_signature,
    ensure_base64_sig,
    trim_gemini_path_prefix,
)
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
    AnonymousVertexRateLimitError,
    AnonymousVertexAuthError,
    AnonymousVertexConnectionError,
    AnonymousVertexProtocolError,
    AnonymousVertexParseError,
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
    assert vreq.contents[0].role == "user"
    assert vreq.contents[0].parts[0].text == "hello"
    assert vreq.safety_settings[0]["threshold"] == "BLOCK_NONE"
    assert vreq.generation_config.max_output_tokens == 65535


def test_request_conversion_system_message():
    req = ChatRequest(
        model="gemini-2.5-flash",
        messages=[
            ChatMessage(role="system", content="You are helpful"),
            ChatMessage(role="user", content="hi"),
        ],
    )
    vreq = chat_to_vertex_request(req)
    assert vreq.system_instruction.parts[0].text == "You are helpful"
    assert vreq.contents[0].role == "user"


def test_request_conversion_assistant_to_model():
    req = ChatRequest(
        model="gemini-2.5-flash",
        messages=[
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="hello there"),
        ],
    )
    vreq = chat_to_vertex_request(req)
    assert vreq.contents[1].role == "model"
    assert vreq.contents[1].parts[0].text == "hello there"


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
    assert len(vreq.contents) == 1
    parts_text = "".join(p.text for p in vreq.contents[0].parts)
    assert parts_text == "ab"


def test_request_conversion_temperature():
    req = make_request()
    req.temperature = 0.7
    vreq = chat_to_vertex_request(req)
    assert vreq.generation_config.temperature == 0.7


def test_request_conversion_max_tokens_clamped():
    req = make_request()
    req.max_tokens = 100000  # above spec max
    vreq = chat_to_vertex_request(req)
    assert vreq.generation_config.max_output_tokens == 65535


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
    assert isinstance(mapped, AnonymousVertexRateLimitError)
    assert isinstance(mapped, AnonymousVertexProtocolError)
    assert isinstance(mapped, RateLimitError)


def test_parse_400_invalid():
    with open(FIXTURES / "error_400.json", encoding="utf-8") as f:
        body = f.read().encode()
    err = parse_upstream_error(400, body)
    assert err.kind == "invalid"
    mapped = classify_upstream_error(err)
    assert isinstance(mapped, InvalidRequestError)
    assert isinstance(mapped, ProviderError)


def test_429_with_retry_after_header():
    # Simulate a 429 response; provider reads Retry-After and passes to RateLimitError
    from providers.anonymous_vertex.errors import UpstreamVertexError
    err = UpstreamVertexError("rate limited", status_code=429, retry_after=10.0)
    mapped = classify_upstream_error(err)
    assert isinstance(mapped, AnonymousVertexRateLimitError)
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

    with pytest.raises(AnonymousVertexRateLimitError) as ei:
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

    with pytest.raises(AnonymousVertexRateLimitError) as ei:
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

    with pytest.raises(AnonymousVertexRateLimitError) as ei:
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
        with pytest.raises(AnonymousVertexParseError):
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
        with pytest.raises(AnonymousVertexParseError):
            async for _ in iter_chunks(gen()):
                pass

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Test 8: Protocol error hierarchy
# ---------------------------------------------------------------------------
def test_protocol_error_subclasses():
    from providers.anonymous_vertex.errors import (
        AnonymousVertexProtocolError,
        AnonymousVertexAuthError,
        AnonymousVertexRateLimitError,
        AnonymousVertexParseError,
        AnonymousVertexConnectionError,
        AnonymousVertexUnavailableError,
    )
    from core.errors import (
        AuthenticationError,
        RateLimitError,
        NetworkError,
        UpstreamUnavailableError,
    )

    # All are ProviderError subclasses
    for cls in [
        AnonymousVertexProtocolError,
        AnonymousVertexAuthError,
        AnonymousVertexRateLimitError,
        AnonymousVertexParseError,
        AnonymousVertexConnectionError,
        AnonymousVertexUnavailableError,
    ]:
        assert issubclass(cls, ProviderError)

    # Cross-hierarchy
    assert issubclass(AnonymousVertexAuthError, AuthenticationError)
    assert issubclass(AnonymousVertexRateLimitError, RateLimitError)
    assert issubclass(AnonymousVertexConnectionError, NetworkError)
    assert issubclass(AnonymousVertexUnavailableError, UpstreamUnavailableError)


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
    with pytest.raises(AnonymousVertexRateLimitError):
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


# ---------------------------------------------------------------------------
# AUDIT-PROVIDER-001: production-readiness audit regression tests
# ---------------------------------------------------------------------------
def test_plain_502_maps_to_unavailable_and_retryable():
    """A bare upstream HTTP 502 is transient: Unavailable + retryable.

    AUDIT-PROVIDER-001: classify_upstream_error previously mapped 502 to
    AnonymousVertexAuthError, which is non-retryable — the Scheduler then
    aborted instead of falling back / cooling down transiently.
    """
    from core.errors import is_retryable
    from providers.anonymous_vertex.errors import (
        AnonymousVertexUnavailableError,
        UpstreamVertexError,
    )

    err = parse_upstream_error(
        502, b'{"error":{"code":502,"message":"Bad Gateway","status":""}}'
    )
    assert err.kind == "server"
    mapped = classify_upstream_error(err)
    assert isinstance(mapped, AnonymousVertexUnavailableError)
    assert not isinstance(mapped, AnonymousVertexAuthError)
    assert is_retryable(mapped)

    # Same for a kind="server" raw error (parse path always sets kind).
    raw = UpstreamVertexError("boom", status_code=502, kind="server")
    mapped2 = classify_upstream_error(raw)
    assert isinstance(mapped2, AnonymousVertexUnavailableError)
    assert is_retryable(mapped2)


def test_stream_frame_auth_error_502_still_auth():
    """Stream-frame auth failures (HTTP 200 + kind="auth") stay auth-class."""
    from core.errors import is_retryable
    from providers.anonymous_vertex.errors import UpstreamVertexError

    err = UpstreamVertexError(
        "Failed to verify action", status_code=502, kind="auth"
    )
    mapped = classify_upstream_error(err)
    assert isinstance(mapped, AnonymousVertexAuthError)
    assert not is_retryable(mapped)


def test_connect_error_maps_to_connection_error():
    """httpx.ConnectError -> AnonymousVertexConnectionError (ProviderError)."""
    import httpx

    class ResetClient:
        async def post(self, url, content=None, headers=None):
            raise httpx.ConnectError("connection reset")

    provider = AnonymousVertexProvider(
        http_client=ResetClient(), token_fetcher=_token_fetcher
    )
    import asyncio

    with pytest.raises(AnonymousVertexConnectionError):
        asyncio.run(provider.complete(make_request(), make_resource()))


def test_timeout_maps_to_timeout_error():
    """httpx.ReadTimeout -> core TimeoutError (retryable ProviderError)."""
    import httpx

    from core.errors import TimeoutError as GatewayTimeoutError
    from core.errors import is_retryable

    class SlowClient:
        async def post(self, url, content=None, headers=None):
            raise httpx.ReadTimeout("timed out")

    provider = AnonymousVertexProvider(
        http_client=SlowClient(), token_fetcher=_token_fetcher
    )
    import asyncio

    with pytest.raises(GatewayTimeoutError) as ei:
        asyncio.run(provider.complete(make_request(), make_resource()))
    assert is_retryable(ei.value)


def test_midstream_reset_maps_to_connection_error():
    """A connection reset after headers are received is also classified."""
    import httpx

    class MidstreamResetResponse:
        status_code = 200
        content = b""

        async def aiter_bytes(self):
            yield b'{"results":[{"data":{"ui":{"streamGenerateContentAnonymous":'
            raise httpx.ReadError("connection reset mid-stream")

    class ResetClient:
        async def post(self, url, content=None, headers=None):
            return MidstreamResetResponse()

    provider = AnonymousVertexProvider(
        http_client=ResetClient(), token_fetcher=_token_fetcher
    )
    import asyncio

    async def run():
        async for _ in provider.stream(make_request(), make_resource()):
            pass

    with pytest.raises(AnonymousVertexConnectionError):
        asyncio.run(run())


def test_recaptcha_failure_maps_to_unavailable():
    """recaptcha anchor/reload failure -> UpstreamUnavailableError.

    A raw RuntimeError escaping the provider would bypass the Scheduler's
    ProviderError bookkeeping (no cooldown, in_flight leak, no fallback).
    """
    import asyncio

    from core.errors import UpstreamUnavailableError, is_retryable

    class FailingRecaptchaClient:
        async def get(self, url):
            raise RuntimeError("recaptcha anchor failed: HTTP 500")

        async def post(self, url, content=None, headers=None):
            raise AssertionError("should not be reached")

    provider = AnonymousVertexProvider(http_client=FailingRecaptchaClient())
    with pytest.raises(UpstreamUnavailableError) as ei:
        asyncio.run(provider.complete(make_request(), make_resource()))
    assert is_retryable(ei.value)


def test_scheduler_falls_back_after_connect_error():
    """Resource A connection-reset -> cooldown bookkeeping -> resource B wins.

    End-to-end Scheduler fallback with the real AnonymousVertexProvider and
    a mock transport: request 1 fails at the wire level, request 2 succeeds
    on the second resource without any provider-owned retry logic.
    """
    import asyncio

    from core.cooldown import CooldownManager
    from core.pool import InMemoryPool
    from core.scheduler import Scheduler

    with open(FIXTURES / "response.json", encoding="utf-8") as f:
        ok_body = f.read().encode()

    import httpx

    class FlakyClient:
        def __init__(self):
            self.calls = 0

        async def post(self, url, content=None, headers=None):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ConnectError("connection reset")
            return MockResponse(200, ok_body)

    provider = AnonymousVertexProvider(
        http_client=FlakyClient(), token_fetcher=_token_fetcher
    )
    resources = [
        AnonymousVertexResource(id="r1", provider="anonymous_vertex"),
        AnonymousVertexResource(id="r2", provider="anonymous_vertex"),
    ]
    pool = InMemoryPool(
        provider="anonymous_vertex",
        resources=resources,
        cooldown=CooldownManager(),
    )
    scheduler = Scheduler(
        providers={"anonymous_vertex": provider},
        pools={"anonymous_vertex": pool},
        max_retries=2,
    )
    resp = asyncio.run(scheduler.chat_completion(make_request()))

    # ANON-004: the node-pool attempt loop absorbs the first wire-level
    # failure (recorded on the node, retried on the next attempt), so the
    # scheduler and the core resource never see it.
    assert "Hello from Anonymous Vertex!" in resp.text
    r1, r2 = pool.resources
    assert r1.total_failures == 0 and r2.total_failures == 0
    node = provider.node_pool.nodes[0]
    assert node.total_failures == 1
    assert node.current_in_flight == 0  # no leaks




# ---------------------------------------------------------------------------
# ANON-002: true incremental streaming over the transport boundary
# ---------------------------------------------------------------------------
def _frame(payload: dict) -> bytes:
    """Wrap a Gemini chunk payload into one batchGraphql NDJSON frame."""
    return json.dumps(
        {"results": [{"data": {"ui": {"streamGenerateContentAnonymous": payload}}}]}
    ).encode()


_FRAME1_TEXT = "Hello"
_FRAME2_TEXT = " world"
_FRAME1 = _frame({"candidates": [{"content": {"role": "model", "parts": [{"text": _FRAME1_TEXT}]}}]})
_FRAME2 = _frame({
    "candidates": [{
        "content": {"role": "model", "parts": [{"text": _FRAME2_TEXT}]},
        "finishReason": "STOP",
    }],
    "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5},
})


class _StreamResponseFake:
    """Queue-driven streaming response (httpx-shaped, 200 path).

    Bytes reach ``aiter_bytes()`` only as they are pushed — exactly like a
    real streaming HTTP body.  ``aread()`` is instrumented so tests can
    prove the 200 path never buffers the body.
    """

    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False
        self.aread_calls = 0
        self._queue = asyncio.Queue()

    async def aiter_bytes(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def aread(self):
        self.aread_calls += 1
        return b""

    async def aclose(self):
        self.closed = True

    def push(self, data):
        self._queue.put_nowait(data)

    def end(self):
        self._queue.put_nowait(None)


class _StaticStreamResponse:
    """Streaming response with a fixed body (non-200 error responses)."""

    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self.closed = False
        self.aread_calls = 0

    async def aiter_bytes(self):
        yield self._body

    async def aread(self):
        self.aread_calls += 1
        return self._body

    async def aclose(self):
        self.closed = True


class _StreamClientFake:
    """httpx.AsyncClient-shaped fake: ``stream()`` returns an async context
    manager yielding the response and closing it on exit — the same shape
    as ``AsyncClient.stream()``, so ``HttpxTransport`` takes the true
    streaming path with it."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.stream_calls = 0

    def _next_response(self):
        if len(self._responses) == 1:
            return self._responses[0]
        return self._responses.pop(0)

    def stream(self, method, url, *, content=None, headers=None):
        self.stream_calls += 1
        resp = self._next_response()

        @asynccontextmanager
        async def cm():
            try:
                yield resp
            finally:
                await resp.aclose()

        return cm()


def _streaming_provider(*responses):
    """Provider whose transport takes the true streaming path."""
    return AnonymousVertexProvider(
        http_client=_StreamClientFake(*responses), token_fetcher=_token_fetcher
    )


def test_stream_content_is_incremental_first_token_before_next_chunk():
    """A+B: the first upstream frame reaches the Provider as soon as its
    bytes arrive — while the NEXT chunk has not been pushed yet.

    A buffered implementation (transport reading the whole body first)
    deadlocks this test: ``got_first`` can only be set before frame2 is
    pushed.
    """
    import asyncio

    resp = _StreamResponseFake()
    provider = _streaming_provider(resp)
    got_first = asyncio.Event()
    collected = []

    async def consume():
        async for chunk in provider.stream(make_request(), make_resource()):
            collected.append(chunk)
            if chunk.text and not got_first.is_set():
                got_first.set()

    async def driver():
        task = asyncio.create_task(consume())
        await asyncio.sleep(0)  # let the consumer reach the read point
        resp.push(_FRAME1)  # frame 1 arrives as ONE network chunk
        await asyncio.wait_for(got_first.wait(), timeout=2.0)
        # First token already yielded to the gateway; frame2 not sent yet.
        assert not any((c.text or "").endswith(_FRAME2_TEXT) for c in collected)
        resp.push(_FRAME2)
        resp.end()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(driver())
    texts = [c.text for c in collected if c.text]
    assert texts == [_FRAME1_TEXT, _FRAME2_TEXT]
    assert resp.aread_calls == 0  # 200 path never buffers the body
    assert resp.closed


def test_partial_json_chunks_are_parsed_incrementally():
    """A: a frame split across two network chunks parses only once complete,
    and the first token still arrives before the next frame is pushed."""
    import asyncio

    resp = _StreamResponseFake()
    provider = _streaming_provider(resp)
    got_first = asyncio.Event()
    collected = []

    async def consume():
        async for chunk in provider.stream(make_request(), make_resource()):
            collected.append(chunk)
            if chunk.text and not got_first.is_set():
                got_first.set()

    async def driver():
        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        raw = _FRAME1
        split = len(raw) // 2
        resp.push(raw[:split])  # partial JSON only
        await asyncio.sleep(0.02)
        assert not collected, "no chunk may be produced from a torn frame"
        resp.push(raw[split:])  # rest of the same frame
        await asyncio.wait_for(got_first.wait(), timeout=2.0)
        resp.push(_FRAME2)
        resp.end()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(driver())
    assert [c.text for c in collected if c.text] == [_FRAME1_TEXT, _FRAME2_TEXT]
    assert resp.closed


def test_midstream_disconnect_maps_to_connection_error():
    """C: chunk1, torn chunk2, connection reset -> ConnectionError."""
    import asyncio

    import httpx

    resp = _StreamResponseFake()
    resp.push(_FRAME1)
    resp.push(_FRAME2[: len(_FRAME2) // 2])  # next frame, torn
    resp.push(httpx.ReadError("connection reset mid-stream"))
    provider = _streaming_provider(resp)

    async def run():
        chunks = []
        with pytest.raises(AnonymousVertexConnectionError):
            async for c in provider.stream(make_request(), make_resource()):
                chunks.append(c)
        return chunks

    chunks = asyncio.run(run())
    assert [c.text for c in chunks if c.text] == [_FRAME1_TEXT]


def test_midstream_disconnect_releases_resource_and_pool_stays_usable():
    """C: after a mid-stream disconnect the resource is released
    (``in_flight`` back to 0, no leak) and the pool serves the next
    request normally."""
    import asyncio

    import httpx

    dead = _StreamResponseFake()
    dead.push(_FRAME1)
    dead.push(httpx.ReadError("connection reset mid-stream"))
    healthy = _StreamResponseFake()
    healthy.push(_FRAME1)
    healthy.push(_FRAME2)
    healthy.end()
    provider = _streaming_provider(dead, healthy)

    from core.cooldown import CooldownManager
    from core.pool import InMemoryPool
    from core.scheduler import Scheduler

    resource = AnonymousVertexResource(id="r1", provider="anonymous_vertex")
    pool = InMemoryPool(
        provider="anonymous_vertex", resources=[resource], cooldown=CooldownManager()
    )
    scheduler = Scheduler(
        providers={"anonymous_vertex": provider},
        pools={"anonymous_vertex": pool},
        max_retries=2,
    )

    async def run():
        with pytest.raises(AnonymousVertexConnectionError):
            async for _ in scheduler.stream_chat(make_request()):
                pass
        assert resource.in_flight == 0  # no leak after the failure
        return await scheduler.chat_completion(make_request())

    result = asyncio.run(run())
    assert _FRAME1_TEXT in result.text
    assert resource.in_flight == 0


def test_disconnect_before_first_chunk_falls_back_to_next_resource():
    """C: a connection reset before any parsed chunk lets the Scheduler
    fall back to resource B; no ``in_flight`` leaks on either resource."""
    import asyncio

    import httpx

    dead = _StreamResponseFake()
    dead.push(httpx.ReadError("connection reset before first chunk"))
    healthy = _StreamResponseFake()
    healthy.push(_FRAME1)
    healthy.push(_FRAME2)
    healthy.end()
    provider = _streaming_provider(dead, healthy)

    from core.cooldown import CooldownManager
    from core.pool import InMemoryPool
    from core.scheduler import Scheduler

    r1 = AnonymousVertexResource(id="r1", provider="anonymous_vertex")
    r2 = AnonymousVertexResource(id="r2", provider="anonymous_vertex")
    pool = InMemoryPool(
        provider="anonymous_vertex", resources=[r1, r2], cooldown=CooldownManager()
    )
    scheduler = Scheduler(
        providers={"anonymous_vertex": provider},
        pools={"anonymous_vertex": pool},
        max_retries=2,
    )

    async def run():
        chunks = []
        async for chunk in scheduler.stream_chat(make_request()):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(run())
    texts = "".join(c.text for c in chunks if c.text)
    assert _FRAME1_TEXT in texts
    # ANON-004: the pre-chunk disconnect was recorded on the node and the
    # attempt loop moved on; scheduler/core resource stayed clean.
    assert r1.total_failures == 0 and r2.total_failures == 0
    node = provider.node_pool.nodes[0]
    assert node.total_failures == 1
    assert r1.in_flight == 0 and r2.in_flight == 0


def test_normal_eof_yields_single_terminal_finish_chunk():
    """D: normal EOF — parser ends, the response is closed, and exactly one
    terminal (empty-delta) finish chunk is emitted with nothing after it."""
    import asyncio

    resp = _StreamResponseFake()
    resp.push(_FRAME1)
    resp.push(_FRAME2)
    resp.end()
    provider = _streaming_provider(resp)

    async def run():
        chunks = []
        async for c in provider.stream(make_request(), make_resource()):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(run())
    assert [c.text for c in chunks if c.text] == [_FRAME1_TEXT, _FRAME2_TEXT]
    terminal = [c for c in chunks if c.finish_reason and not c.text]
    assert len(terminal) == 1  # exactly one terminal finish chunk
    assert terminal[0] is chunks[-1]  # nothing follows it
    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].usage is not None
    assert resp.aread_calls == 0 and resp.closed


def test_stream_error_status_via_streaming_transport():
    """E: 429 + Retry-After / 502 / 503 keep their classification through
    the streaming transport (small error body read, response closed)."""
    import asyncio

    from providers.anonymous_vertex.errors import AnonymousVertexUnavailableError

    # ANON-004: a 429 cools its node, so each status gets its own provider.
    r429 = _StaticStreamResponse(
        429,
        b'{"error":{"code":429,"message":"rate","status":"RESOURCE_EXHAUSTED"}}',
        headers={"Retry-After": "7"},
    )
    r502 = _StaticStreamResponse(502, b'{"error":{"code":502,"message":"bad gateway"}}')
    r503 = _StaticStreamResponse(503, b'{"error":{"code":503,"message":"unavailable"}}')

    async def run():
        with pytest.raises(AnonymousVertexRateLimitError) as ei:
            await _streaming_provider(r429).complete(make_request(), make_resource())
        assert ei.value.retry_after == 7.0
        assert r429.aread_calls == 1 and r429.closed

        with pytest.raises(AnonymousVertexUnavailableError):
            await _streaming_provider(r502).complete(make_request(), make_resource())
        assert r502.closed

        with pytest.raises(AnonymousVertexUnavailableError):
            await _streaming_provider(r503).complete(make_request(), make_resource())
        assert r503.closed

    asyncio.run(run())


def test_cancellation_closes_stream_response():
    """F: cancelling a consumer parked mid-stream closes the HTTP response."""
    import asyncio
    import contextlib

    resp = _StreamResponseFake()  # no data ever: consumer blocks on read
    closed_by_cancel = asyncio.Event()
    provider = _streaming_provider(resp)

    async def consume():
        try:
            async for _ in provider.stream(make_request(), make_resource()):
                pass
        finally:
            if resp.closed:
                closed_by_cancel.set()

    async def run():
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)  # consumer is now parked on the body read
        assert not resp.closed
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)
        await asyncio.wait_for(closed_by_cancel.wait(), timeout=2.0)

    asyncio.run(run())
    assert resp.closed


def test_stream_entry_connect_error_maps_to_connection_error():
    """A connect failure raised while opening the stream is classified."""
    import asyncio

    import httpx

    class _ConnectFailClient:
        def stream(self, method, url, *, content=None, headers=None):
            @asynccontextmanager
            async def cm():
                raise httpx.ConnectError("no route to host")
                yield  # pragma: no cover

            return cm()

    provider = AnonymousVertexProvider(
        http_client=_ConnectFailClient(), token_fetcher=_token_fetcher
    )

    async def run():
        async for _ in provider.stream(make_request(), make_resource()):
            pass

    with pytest.raises(AnonymousVertexConnectionError):
        asyncio.run(run())


def test_stream_timeout_maps_to_timeout_error():
    """A read stall mid-stream maps to the retryable TimeoutError."""
    import asyncio

    import httpx

    resp = _StreamResponseFake()
    resp.push(_FRAME1)
    resp.push(httpx.ReadTimeout("timed out mid-stream"))
    provider = _streaming_provider(resp)

    from core.errors import TimeoutError as GatewayTimeoutError

    async def run():
        async for _ in provider.stream(make_request(), make_resource()):
            pass

    with pytest.raises(GatewayTimeoutError):
        asyncio.run(run())
