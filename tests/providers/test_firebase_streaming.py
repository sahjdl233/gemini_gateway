"""TASK-004 tests: Firebase SSE upstream -> SSE events -> ChatChunk.

The upstream wire format is SSE (data: {...}, terminated by data: [DONE]),
NOT Anonymous Vertex NDJSON — a dedicated parser is required (TASK-003).
"""
from __future__ import annotations

import json

from providers.firebase.response import parse_chunk
from providers.firebase.streaming import iter_chunks, iter_sse_events

from tests.providers._firebase_fakes import FakeResponse, make_sse


MODEL = "gemini-3.8-flash"


def text_event(text: str, finish_reason: str = "STOP") -> dict:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": finish_reason,
            }
        ]
    }


async def drain(agen):
    return [item async for item in agen]


async def test_sse_single_chunk():
    resp = FakeResponse(200, sse_chunks=[make_sse(text_event("hi"))])
    events = await drain(iter_sse_events(resp))
    assert len(events) == 1
    assert events[0]["candidates"][0]["content"]["parts"][0]["text"] == "hi"


async def test_sse_multiple_chunks_and_done():
    body = make_sse(text_event("hello "), text_event("world"), "[DONE]")
    resp = FakeResponse(200, sse_chunks=[body])
    events = await drain(iter_sse_events(resp))
    assert [e["candidates"][0]["content"]["parts"][0]["text"] for e in events] == [
        "hello ",
        "world",
    ]


async def test_sse_split_across_network_chunks():
    body = make_sse(text_event("hello "), text_event("world"), "[DONE]")
    mid = len(body) // 2
    resp = FakeResponse(200, sse_chunks=[body[:mid], body[mid:]])
    events = await drain(iter_sse_events(resp))
    assert len(events) == 2


async def test_sse_finish_reason_and_usage_final_chunk():
    body = make_sse(
        text_event("done"),
        {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": ""}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"totalTokenCount": 42},
        },
        "[DONE]",
    )
    chunks = await drain(iter_chunks(FakeResponse(200, sse_chunks=[body]), MODEL))
    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.total_tokens == 42


async def test_sse_tool_call_chunk():
    body = make_sse(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"functionCall": {"name": "f", "args": {"a": 1}}}],
                    },
                    "finishReason": "STOP",
                }
            ]
        },
        "[DONE]",
    )
    chunks = await drain(iter_chunks(FakeResponse(200, sse_chunks=[body]), MODEL))
    assert len(chunks) == 1
    assert chunks[0].tool_calls[0]["function"]["name"] == "f"


async def test_sse_malformed_line_is_skipped():
    body = b"data: not-json\n\ndata: " + json.dumps(text_event("ok")).encode() + b"\n\ndata: [DONE]\n\n"
    events = await drain(iter_sse_events(FakeResponse(200, sse_chunks=[body])))
    assert len(events) == 1
    assert events[0]["candidates"][0]["content"]["parts"][0]["text"] == "ok"


async def test_sse_only_done_yields_nothing():
    events = await drain(iter_sse_events(FakeResponse(200, sse_chunks=[make_sse("[DONE]")])))
    assert events == []


async def test_iter_sse_events_requires_data_prefix():
    body = b"ping: x\n\ndata: " + json.dumps(text_event("ok")).encode() + b"\n\n"
    events = await drain(iter_sse_events(FakeResponse(200, sse_chunks=[body])))
    assert len(events) == 1

