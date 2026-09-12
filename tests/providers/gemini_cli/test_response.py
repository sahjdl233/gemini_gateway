"""Code Assist response envelope parsing (TASK-008)."""
from __future__ import annotations

from core.models import ChatResponse, ChatChunk, Usage
from providers.gemini_cli.response import (
    parse_response,
    parse_chunk,
    unwrap_envelope,
    extract_trace_id,
    _parts_to_text_and_tools,
    _map_finish_reason,
    _parse_usage,
)


def test_unwrap_envelope():
    env = {"response": {"candidates": []}, "traceId": "t-1"}
    assert unwrap_envelope(env) == {"candidates": []}


def test_extract_trace_id():
    env = {"response": {}, "traceId": "abc123"}
    assert extract_trace_id(env) == "abc123"


def test_parse_response_non_streaming():
    env = {
        "response": {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "Hello world"}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
                "thoughtsTokenCount": 0,
                "totalTokenCount": 15,
            },
        },
        "traceId": "trace-xyz",
    }
    resp = parse_response(env, "gemini-2.5-flash")
    assert isinstance(resp, ChatResponse)
    assert resp.text == "Hello world"
    assert resp.finish_reason == "stop"
    assert resp.usage.prompt_tokens == 10
    assert resp.usage.completion_tokens == 5
    assert resp.usage.total_tokens == 15


def test_parse_response_with_tool_calls():
    env = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"functionCall": {"name": "get_weather", "args": {"loc": "Tokyo"}}}
                        ],
                    },
                    "finishReason": "STOP",
                }
            ]
        }
    }
    resp = parse_response(env, "gemini-2.5-flash")
    assert resp.text == ""
    assert resp.tool_calls is not None
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0]["function"]["name"] == "get_weather"


def test_parse_chunk_streaming():
    chunk_data = {
        "response": {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "Hello"}]},
                }
            ]
        }
    }
    chunk = parse_chunk(chunk_data, "gemini-2.5-flash")
    assert isinstance(chunk, ChatChunk)
    assert chunk.text == "Hello"
    assert chunk.finish_reason is None


def test_parse_chunk_with_finish_reason():
    chunk_data = {
        "response": {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": " done"}]},
                    "finishReason": "MAX_TOKENS",
                }
            ]
        }
    }
    chunk = parse_chunk(chunk_data, "gemini-2.5-flash")
    assert chunk.finish_reason == "length"
    assert chunk.text == " done"


def test_parse_chunk_with_usage_only():
    chunk_data = {
        "response": {
            "candidates": [],
            "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 2, "totalTokenCount": 10}
        }
    }
    chunk = parse_chunk(chunk_data, "gemini-2.5-flash")
    assert chunk.text is None
    assert chunk.usage is not None
    assert chunk.usage.prompt_tokens == 8
    assert chunk.usage.completion_tokens == 2


def test_parse_chunk_thought_stripped():
    chunk_data = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": "public", "thought": False}, {"text": "internal", "thought": True}]
                    }
                }
            ]
        }
    }
    chunk = parse_chunk(chunk_data, "gemini-2.5-flash")
    assert chunk.text == "public"
    assert "internal" not in chunk.text


def test_parts_to_text_and_tools():
    parts = [
        {"text": "Hello "},
        {"functionCall": {"name": "foo", "args": {"a": 1}}},
        {"text": " world"},
    ]
    text, tools = _parts_to_text_and_tools(parts)
    assert text == "Hello  world"
    assert tools is not None
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "foo"


def test_map_finish_reason():
    assert _map_finish_reason("STOP") == "stop"
    assert _map_finish_reason("MAX_TOKENS") == "length"
    assert _map_finish_reason("SAFETY") == "content_filter"
    assert _map_finish_reason("RECITATION") == "content_filter"
    assert _map_finish_reason("UNKNOWN") is None
    assert _map_finish_reason(None) is None


def test_parse_usage_thoughts_added_to_completion():
    meta = {
        "promptTokenCount": 10,
        "candidatesTokenCount": 5,
        "thoughtsTokenCount": 3,
        "totalTokenCount": 18,
    }
    usage = _parse_usage(meta)
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 8  # candidates + thoughts
    assert usage.total_tokens == 18

