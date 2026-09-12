"""GCLI SSE parser (TASK-008).
- No [DONE] sentinel from upstream
- Envelope {response: ..., traceId: ...} must be unwrapped
- Network chunk fragmentation handled
"""
from __future__ import annotations

from providers.gemini_cli.streaming import iter_sse_events, iter_chunks


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

