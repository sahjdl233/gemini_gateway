"""TASK-004 tests: Gemini response -> ChatResponse / ChatChunk."""
from __future__ import annotations

from providers.firebase.response import parse_chunk, parse_response


MODEL = "gemini-3.8-flash"


def sample_response(**overrides):
    data = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "Hello!"}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 12,
            "candidatesTokenCount": 5,
            "thoughtsTokenCount": 0,
            "totalTokenCount": 17,
        },
    }
    data.update(overrides)
    return data


def test_plain_text_response():
    resp = parse_response(sample_response(), MODEL)
    assert resp.text == "Hello!"
    assert resp.finish_reason == "stop"
    assert resp.usage is not None
    assert resp.usage.prompt_tokens == 12
    assert resp.usage.completion_tokens == 5
    assert resp.usage.total_tokens == 17


def test_tool_call_response():
    data = sample_response(
        candidates=[
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "name": "get_weather",
                                "args": {"city": "London"},
                            }
                        }
                    ],
                },
                "finishReason": "STOP",
            }
        ]
    )
    resp = parse_response(data, MODEL)
    assert resp.text == ""
    assert resp.tool_calls is not None
    assert resp.tool_calls[0]["function"]["name"] == "get_weather"
    assert resp.tool_calls[0]["function"]["arguments"] == '{"city": "London"}'


def test_thought_parts_are_stripped():
    data = sample_response(
        candidates=[
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"thought": True, "text": "thinking...", "thoughtSignature": "sig"},
                        {"text": "final answer"},
                    ],
                },
                "finishReason": "STOP",
            }
        ]
    )
    resp = parse_response(data, MODEL)
    assert resp.text == "final answer"
    assert "thinking" not in resp.text


def test_finish_reason_mapping() -> None:
    cases = {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
        "UNKNOWN_REASON": "stop",
    }
    for raw, expected in cases.items():
        resp = parse_response(sample_response(candidates=[{
            "content": {"role": "model", "parts": [{"text": "x"}]},
            "finishReason": raw,
        }]), MODEL)
        assert resp.finish_reason == expected


def test_empty_response_returns_empty_chat_response():
    resp = parse_response({"candidates": [], "usageMetadata": {}}, MODEL)
    assert resp.text == ""
    assert resp.finish_reason == "stop"
    assert resp.usage is None or resp.usage.total_tokens == 0


def test_usage_includes_thoughts_tokens():
    data = sample_response(
        usageMetadata={
            "promptTokenCount": 10,
            "candidatesTokenCount": 20,
            "thoughtsTokenCount": 7,
            "totalTokenCount": 37,
        }
    )
    resp = parse_response(data, MODEL)
    assert resp.usage.completion_tokens == 27  # candidates + thoughts
    assert resp.usage.total_tokens == 37


def test_parse_chunk_single_text():
    chunk = parse_chunk(sample_response(), MODEL)
    assert chunk is not None
    assert chunk.text == "Hello!"
    assert chunk.finish_reason == "stop"


def test_parse_chunk_tool_call():
    chunk = parse_chunk(
        sample_response(candidates=[{
            "content": {
                "role": "model",
                "parts": [{"functionCall": {"name": "f", "args": {}}}],
            },
            "finishReason": "STOP",
        }]),
        MODEL,
    )
    assert chunk.tool_calls is not None
    assert chunk.tool_calls[0]["function"]["name"] == "f"


def test_parse_chunk_usage_only():
    chunk = parse_chunk({"usageMetadata": {"totalTokenCount": 99}}, MODEL)
    assert chunk is not None
    assert chunk.text is None
    assert chunk.usage is not None
    assert chunk.usage.total_tokens == 99


def test_parse_chunk_empty_returns_none():
    assert parse_chunk({"candidates": []}, MODEL) is None

