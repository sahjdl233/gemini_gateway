"""GCLI SSE parser (TASK-008).
- No [DONE] sentinel from upstream
- Envelope {response: ..., traceId: ...} must be unwrapped
- Network chunk fragmentation handled
"""
from __future__ import annotations

from providers.gemini_cli.streaming import iter_sse_events, iter_chunks
from tests.providers._gemini_cli_fakes import FakeHttp, FakeResponse, make_resource


class FakeStream:
    def __init__(self, *chunks: bytes):
        self._chunks = list(chunks)

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


def chunk_body(text):
    return {
        "response": {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}]},
        "traceId": "t",
    }


def finish_body(reason="STOP"):
    return {
        "response": {"candidates": [{"content": {"role": "model", "parts": [{"text": ""}]}, "finishReason": reason}]},
    }


def sse_bytes(*events):
    import json
    parts = []
    for e in events:
        if e == "DONE":
            parts.append("data: [DONE]")
        else:
            parts.append("data: " + json.dumps(e, ensure_ascii=False))
    return ("\n\n".join(parts) + "\n\n").encode("utf-8")


async def test_iter_sse_events_basic():
    body = sse_bytes(chunk_body("Hi"), chunk_body(" there"))
    stream = FakeStream(body)
    events = []
    async for ev in iter_sse_events(stream):
        events.append(ev)
    for e in events:
        print("EVENT KEYS:", list(e.keys()))
        if "response" in e:
            print("  response keys:", list(e["response"].keys()))
        if "traceId" in e:
            print("  traceId:", e["traceId"])


async def test_iter_sse_events_no_done_sentinel():
    body = sse_bytes(chunk_body("end")) + b"\n\n"
    stream = FakeStream(body)
    events = []
    async for ev in iter_sse_events(stream):
        events.append(ev)
    assert len(events) == 1


async def test_iter_sse_ignores_unexpected_done():
    body = sse_bytes(chunk_body("a"), "DONE", chunk_body("b"))
    stream = FakeStream(body)
    events = []
    async for ev in iter_sse_events(stream):
        events.append(ev)
    assert len(events) == 2
    assert events[0]["response"]["candidates"][0]["content"]["parts"][0]["text"] == "a"
    assert events[1]["response"]["candidates"][0]["content"]["parts"][0]["text"] == "b"


async def test_iter_sse_network_fragmentation():
    body1 = b'data: {"response":{"candidates":[{"content":{"parts":[{"text":"'
    body2 = b'partial'
    body3 = b'"}]}}]}}\n\n'
    body4 = b'data: {"response":{"candidates":[{"content":{"parts":[{"text":"full"}]}}]}}\n\n'
    stream = FakeStream(body1, body2, body3, body4)
    events = []
    async for ev in iter_sse_events(stream):
        events.append(ev)
    assert len(events) == 2
    assert events[0]["response"]["candidates"][0]["content"]["parts"][0]["text"] == "partial"
    assert events[1]["response"]["candidates"][0]["content"]["parts"][0]["text"] == "full"


async def test_iter_sse_multiple_events_per_chunk():
    body = sse_bytes(chunk_body("1"), chunk_body("2"), chunk_body("3"))
    stream = FakeStream(body)
    events = []
    async for ev in iter_sse_events(stream):
        events.append(ev)
    assert len(events) == 3


async def test_iter_chains_into_chunks():
    body = sse_bytes(chunk_body("chunk1"), chunk_body("chunk2"), finish_body("STOP"))
    stream = FakeStream(body)
    chunks = []
    async for ch in iter_chunks(stream, "gemini-2.5-flash"):
        chunks.append(ch)
    assert len(chunks) == 3
    assert chunks[0].text == "chunk1"
    assert chunks[1].text == "chunk2"
    assert chunks[2].finish_reason == "stop"


async def test_iter_chunks_empty_envelope_skipped():
    body = sse_bytes({"response": {}, "traceId": "t1"}, chunk_body("valid"))
    stream = FakeStream(body)
    chunks = []
    async for ch in iter_chunks(stream, "gemini-2.5-flash"):
        chunks.append(ch)
    assert len(chunks) == 1
    assert chunks[0].text == "valid"




# ---------------------------------------------------------------------------
# TASK-009: streaming integration tests (iter_chunks, SSE parsing, OpenAI output)
# ---------------------------------------------------------------------------


class _AuthFakeClock:
    def __init__(self, start=1000.0):
        self.value = start

    def time(self):
        return self.value


def _provider_with_stream(http):
    """Create a GeminiCliProvider with injected FakeHttp for streaming."""
    from providers.gemini_cli.auth import GeminiCliAuth
    from providers.gemini_cli.client import GeminiCliClient

    auth = GeminiCliAuth(http, clock=_AuthFakeClock())
    client = GeminiCliClient(http=http, auth=auth)
    from providers.gemini_cli.provider import GeminiCliProvider
    provider = GeminiCliProvider(models=["gemini-2.5-flash"])
    provider._clients.clear()
    provider._http = http
    original_client_for = provider._client_for
    async def _mock_client_for(resource):
        return client
    provider._client_for = _mock_client_for
    return provider


def _sse_envelope(text_or_parts, *, finish_reason=None, usage=None):
    """Build a Code Assist SSE envelope."""
    candidates = []
    if isinstance(text_or_parts, str):
        candidates = [{"content": {"parts": [{"text": text_or_parts}]}}]
    else:
        candidates = text_or_parts
    env = {"response": {"candidates": candidates}, "traceId": "t1"}
    if finish_reason:
        env["response"]["candidates"][0]["finishReason"] = finish_reason
    if usage:
        env["response"]["usageMetadata"] = usage
    import json
    return "data: " + json.dumps(env) + "\n\n"


async def test_stream_multiple_sse_events_joined():
    """Multiple SSE events -> provider yields concatenated text chunks."""
    http = FakeHttp()
    http.responses.append(http.token_ok("token-1", 3600))
    http.stream_responses.append(
        FakeResponse(
            200,
            sse_chunks=[
                _sse_envelope("Hello").encode("utf-8"),
                _sse_envelope(" world").encode("utf-8"),
                _sse_envelope("", finish_reason="STOP", usage={"promptTokenCount": 10, "candidatesTokenCount": 5}).encode("utf-8"),
            ],
        )
    )
    provider = _provider_with_stream(http)
    resource = make_resource(project_id="proj-1")

    from core.models import ChatRequest, ChatMessage
    req = ChatRequest(model="gemini-2.5-flash", messages=[ChatMessage(role="user", content="hi")], stream=True)
    chunks = [chunk async for chunk in provider.stream(req, resource)]

    # Should have 3 chunks: 2 text deltas + 1 finish
    texts = [c.text for c in chunks if c.text is not None and c.text != ""]
    assert texts == ["Hello", " world"]
    # Last chunk has finish_reason
    assert chunks[-1].finish_reason == "stop"
    # Usage only on final chunk
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.prompt_tokens == 10
    assert chunks[-1].usage.completion_tokens == 5


async def test_stream_single_event_split_across_network_chunks():
    """One SSE event split into multiple network byte chunks."""
    http = FakeHttp()
    http.responses.append(http.token_ok("token-1", 3600))
    # Split one event across 3 chunks
    full_event = _sse_envelope("complete text", finish_reason="STOP")
    # Split at arbitrary points
    parts = [full_event[:10], full_event[10:20], full_event[20:]]
    http.stream_responses.append(
        FakeResponse(
            200,
            sse_chunks=[p.encode("utf-8") for p in parts],
        )
    )
    provider = _provider_with_stream(http)
    resource = make_resource(project_id="proj-1")

    from core.models import ChatRequest, ChatMessage
    req = ChatRequest(model="gemini-2.5-flash", messages=[ChatMessage(role="user", content="hi")], stream=True)
    chunks = [chunk async for chunk in provider.stream(req, resource)]

    assert chunks[0].text == "complete text"
    assert chunks[0].finish_reason == "stop"


async def test_stream_empty_envelope_skipped():
    """Empty envelope (only traceId) produces no chunk."""
    http = FakeHttp()
    http.responses.append(http.token_ok("token-1", 3600))
    http.stream_responses.append(
        FakeResponse(
            200,
            sse_chunks=[
                b"data: {\"response\": {}, \"traceId\": \"t1\"}\n\n",
                _sse_envelope("real").encode("utf-8"),
            ],
        )
    )
    provider = _provider_with_stream(http)
    resource = make_resource(project_id="proj-1")

    from core.models import ChatRequest, ChatMessage
    req = ChatRequest(model="gemini-2.5-flash", messages=[ChatMessage(role="user", content="hi")], stream=True)
    chunks = [chunk async for chunk in provider.stream(req, resource)]

    # Only one chunk for "real"
    assert len(chunks) == 1
    assert chunks[0].text == "real"


async def test_stream_thinking_not_surfaced():
    """Thinking content (part.thought=true) is not surfaced to OpenAI."""
    http = FakeHttp()
    http.responses.append(http.token_ok("token-1", 3600))
    http.stream_responses.append(
        FakeResponse(
            200,
            sse_chunks=[
                _sse_envelope([{"content": {"parts": [{"text": "visible"}, {"thought": True, "text": "thinking"}]}}]).encode("utf-8"),
                _sse_envelope("", finish_reason="STOP").encode("utf-8"),
            ],
        )
    )
    provider = _provider_with_stream(http)
    resource = make_resource(project_id="proj-1")

    from core.models import ChatRequest, ChatMessage
    req = ChatRequest(model="gemini-2.5-flash", messages=[ChatMessage(role="user", content="hi")], stream=True)
    chunks = [chunk async for chunk in provider.stream(req, resource)]

    assert chunks[0].text == "visible"
    assert "thinking" not in str(chunks[0].text)
