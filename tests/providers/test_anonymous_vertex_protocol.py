"""TASK-002-A protocol-layer tests.

Covers:
  - Streaming fixtures A-E (Section 12)
  - Protocol snapshot test: internal Request -> GraphQL payload == fixture (Section 13)
  - Protocol error classification (Section 14)
  - Layering: Provider -> Client -> Protocol -> Transport (Section 18)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.errors import ProviderError, RateLimitError, NetworkError
from core.models import ChatMessage, ChatRequest

from providers.anonymous_vertex.client import AnonymousVertexClient
from providers.anonymous_vertex.errors import (
    AnonymousVertexAuthError,
    AnonymousVertexConnectionError,
    AnonymousVertexParseError,
    AnonymousVertexProtocolError,
    AnonymousVertexRateLimitError,
    AnonymousVertexUnavailableError,
    classify_upstream_error,
    parse_upstream_error,
)
from providers.anonymous_vertex.protocol import (
    build_graphql_payload,
)
from providers.anonymous_vertex.request import chat_to_vertex_request
from providers.anonymous_vertex.models import AnonymousVertexRequestContext
from providers.anonymous_vertex.streaming import (
    StreamingObjectScanner,
    extract_chunk_from_frame,
    iter_chunks,
)
from providers.anonymous_vertex.transport import HttpxTransport
from providers.anonymous_vertex.recaptcha import (
    FakeRecaptchaTokenProvider,
    RecaptchaTokenProvider,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "anonymous_vertex"


def make_request(model="gemini-2.5-flash", content="hello"):
    return ChatRequest(
        model=model,
        messages=[ChatMessage(role="user", content=content)],
    )


# ---------------------------------------------------------------------------
# Fixture A: one HTTP chunk -> one JSON object
# ---------------------------------------------------------------------------
def test_fixture_a_single_object():
    data = (FIXTURES / "stream_fixture_a_single.txt").read_bytes()
    scanner = StreamingObjectScanner()
    objs = scanner.feed(data)
    assert len(objs) == 1
    chunk = extract_chunk_from_frame(objs[0])
    assert chunk["candidates"][0]["content"]["parts"][0]["text"] == "Single object."


# ---------------------------------------------------------------------------
# Fixture B: one JSON object split across multiple HTTP chunks
# ---------------------------------------------------------------------------
def test_fixture_b_split_object():
    data = (FIXTURES / "stream_fixture_b_split.txt").read_bytes()
    scanner = StreamingObjectScanner()
    cut = data.index(b"Split")
    objs1 = scanner.feed(data[:cut])
    objs2 = scanner.feed(data[cut:])
    all_objs = objs1 + objs2
    assert len(all_objs) == 1
    chunk = extract_chunk_from_frame(all_objs[0])
    assert chunk["candidates"][0]["content"]["parts"][0]["text"] == "Split across chunks."


# ---------------------------------------------------------------------------
# Fixture C: multiple consecutive JSON objects in one feed
# ---------------------------------------------------------------------------
def test_fixture_c_multiple_objects():
    data = (FIXTURES / "stream_fixture_c_multiple.txt").read_bytes()
    scanner = StreamingObjectScanner()
    objs = scanner.feed(data)
    assert len(objs) == 3
    first = extract_chunk_from_frame(objs[0])
    second = extract_chunk_from_frame(objs[1])
    assert first["candidates"][0]["content"]["parts"][0]["text"] == "First."
    assert second["candidates"][0]["content"]["parts"][0]["text"] == "Second."
    third = extract_chunk_from_frame(objs[2])
    assert third["candidates"][0]["finishReason"] == "STOP"


# ---------------------------------------------------------------------------
# Fixture D: objects separated by whitespace / newlines
# ---------------------------------------------------------------------------
def test_fixture_d_whitespace_between_objects():
    data = (FIXTURES / "stream_fixture_d_whitespace.txt").read_bytes()
    scanner = StreamingObjectScanner()
    objs = scanner.feed(data)
    assert len(objs) == 2
    first = extract_chunk_from_frame(objs[0])
    assert first["candidates"][0]["content"]["parts"][0]["text"] == "Whitespace."
    second = extract_chunk_from_frame(objs[1])
    assert second["candidates"][0]["finishReason"] == "STOP"


# ---------------------------------------------------------------------------
# Fixture E: incomplete JSON (wait for more data, no crash, no bad chunk)
# ---------------------------------------------------------------------------
def test_fixture_e_incomplete_json_waiting():
    data = (FIXTURES / "stream_fixture_e_incomplete.txt").read_bytes()
    scanner = StreamingObjectScanner()
    objs = scanner.feed(data)
    assert objs == []
    # Complete the truncated object: close "finishReason", then all open braces.
    tail = b'"STOP"}]}]}}}}]}'
    objs2 = scanner.feed(tail)
    assert len(objs2) == 1


def test_fixture_e_at_eof_raises():
    data = (FIXTURES / "stream_fixture_e_incomplete.txt").read_bytes()

    async def gen():
        yield data

    async def run():
        with pytest.raises(AnonymousVertexParseError):
            async for _ in iter_chunks(gen()):
                pass

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Protocol snapshot: internal Request -> GraphQL payload (Section 13)
# ---------------------------------------------------------------------------
def test_request_snapshot_matches_fixture():
    req = make_request()
    vertex_request = chat_to_vertex_request(req)
    context = AnonymousVertexRequestContext(
        page_view_id=1000000000000000,
        tracking_id="d0000000000000000",
        client_session_id="00000000-0000-4000-8000-000000000000",
    )
    payload = build_graphql_payload(
        vertex_request,
        recaptcha_token="RECAPTCHA_TOKEN_PLACEHOLDER",
        request_context=context,
    )
    snapshot = json.loads((FIXTURES / "request.json").read_text(encoding="utf-8"))
    assert payload.to_dict() == snapshot


def test_stream_snapshot_roundtrip():
    snapshot = (FIXTURES / "stream.json").read_text(encoding="utf-8")

    async def gen():
        for line in snapshot.splitlines():
            if line.strip():
                yield line.encode()

    async def run():
        chunks = []
        async for chunk in iter_chunks(gen()):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(run())
    assert len(chunks) == 3
    all_text = []
    for c in chunks:
        items = c if isinstance(c, list) else [c]
        for item in items:
            if not isinstance(item, dict):
                continue
            cands = item.get("candidates") or []
            for cand in cands:
                content = cand.get("content") or {}
                for p in content.get("parts", []):
                    if p.get("text"):
                        all_text.append(p["text"])
    assert "".join(all_text) == "Hello world!"


def test_serialize_payload_roundtrip():
    from providers.anonymous_vertex.protocol import serialize_payload

    req = make_request()
    vertex_request = chat_to_vertex_request(req)
    payload = build_graphql_payload(vertex_request, "tok")
    raw = serialize_payload(payload)
    parsed = json.loads(raw)
    assert parsed["querySignature"] == "2/l8eCsMMY49imcDQ/lwwXyL8cYtTjxZBF2dNqy69LodY="
    assert parsed["operationName"] == "StreamGenerateContentAnonymous"
    assert parsed["variables"]["model"] == "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# Protocol error classification (Section 14)
# ---------------------------------------------------------------------------
def test_protocol_error_classification_full_matrix():
    from providers.anonymous_vertex.errors import UpstreamVertexError
    from core.errors import AuthenticationError, UpstreamUnavailableError, AuthorizationError, InvalidRequestError, ModelNotFoundError

    cases = [
        (429, "ratelimit", AnonymousVertexRateLimitError, RateLimitError),
        (401, "auth", AnonymousVertexAuthError, AuthenticationError),
        (0, "network", AnonymousVertexConnectionError, NetworkError),
        (503, "unavailable", AnonymousVertexUnavailableError, UpstreamUnavailableError),
        (500, "internal", AnonymousVertexUnavailableError, UpstreamUnavailableError),
    ]
    for code, kind, expected, core_expected in cases:
        err = UpstreamVertexError("msg", status_code=code, kind=kind)
        mapped = classify_upstream_error(err)
        assert isinstance(mapped, expected), f"{code}/{kind} -> {type(mapped).__name__}"
        assert isinstance(mapped, core_expected)
    assert isinstance(classify_upstream_error(UpstreamVertexError("x", status_code=403, kind="permission")), AuthorizationError)
    assert isinstance(classify_upstream_error(UpstreamVertexError("x", status_code=400, kind="invalid")), InvalidRequestError)
    assert isinstance(classify_upstream_error(UpstreamVertexError("x", status_code=404, kind="notfound")), ModelNotFoundError)


# ---------------------------------------------------------------------------
# Layering: Client uses Transport interface (no GraphQL in provider)
# ---------------------------------------------------------------------------
class _TransportRecorder:
    """A transport that records the posting envelope."""

    def __init__(self, response):
        self._response = response
        self.posted = []

    async def post(self, url, content=None, headers=None):
        self.posted.append({"url": url, "content": content, "headers": headers})
        return self._response


class _Resp:
    def __init__(self, status_code=200, body=b"{}"):
        self.status_code = status_code
        self.content = body
        self.headers = {}

    async def aiter_bytes(self):
        yield self.content


def test_client_builds_envelope_and_posts_via_transport():
    resp = _Resp(200, (FIXTURES / "stream_fixture_a_single.txt").read_bytes())
    transport = _TransportRecorder(resp)
    client = AnonymousVertexClient(transport, api_key="secret-key")
    req_obj = chat_to_vertex_request(make_request())

    async def run():
        frames = []
        async for f in client.stream_content(req_obj, "tok"):
            frames.append(f)
        return frames

    frames = asyncio.run(run())
    assert transport.posted, "transport should have been called"
    call = transport.posted[0]
    assert "cloudconsole-pa.clients6.google.com" in call["url"]
    body = json.loads(call["content"].decode())
    assert body["querySignature"] == "2/l8eCsMMY49imcDQ/lwwXyL8cYtTjxZBF2dNqy69LodY="
    assert body["operationName"] == "StreamGenerateContentAnonymous"
    assert body["variables"]["recaptchaToken"] == "tok"
    assert len(frames) == 1


def test_httpx_transport_adapter():
    calls = []

    class _Client:
        async def post(self, url, content=None, headers=None):
            calls.append((url, content, headers))
            return _Resp(200)

    transport = HttpxTransport(_Client())
    asyncio.run(transport.post("http://x", content=b"c"))
    assert len(calls) == 1
    assert calls[0][:2] == ("http://x", b"c")


def test_client_no_retry_on_429():
    """429 is classified, never auto-retried (TASK-002-A s15)."""
    body = b'{"error":{"code":429,"message":"rate","status":"RESOURCE_EXHAUSTED"}}'
    transport = _TransportRecorder(_Resp(429, body))
    client = AnonymousVertexClient(transport, api_key="key")
    req_obj = chat_to_vertex_request(make_request())

    async def run():
        with pytest.raises(RateLimitError):
            async for _ in client.stream_content(req_obj, "tok"):
                pass

    asyncio.run(run())
    assert len(transport.posted) == 1, "must not retry on 429"


def test_client_exposes_protocol_error_types():
    assert issubclass(AnonymousVertexProtocolError, ProviderError)
    assert issubclass(AnonymousVertexParseError, ProviderError)
    assert issubclass(AnonymousVertexRateLimitError, AnonymousVertexProtocolError)
    assert issubclass(AnonymousVertexAuthError, AnonymousVertexProtocolError)
    assert issubclass(AnonymousVertexConnectionError, AnonymousVertexProtocolError)
    assert issubclass(AnonymousVertexUnavailableError, AnonymousVertexProtocolError)


# ---------------------------------------------------------------------------
# Recaptcha token provider abstraction (Section 17)
# ---------------------------------------------------------------------------
def test_recaptcha_token_provider_protocol():
    assert isinstance(FakeRecaptchaTokenProvider(), RecaptchaTokenProvider)


def test_fake_recaptcha_token_provider_returns_fixed_token():
    provider = FakeRecaptchaTokenProvider("tok-123")
    assert asyncio.run(provider.get_token()) == "tok-123"
    assert provider.calls == 1


def test_fake_recaptcha_default_token():
    provider = FakeRecaptchaTokenProvider()
    assert asyncio.run(provider.get_token()) == "recaptcha-token"
