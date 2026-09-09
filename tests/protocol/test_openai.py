"""OpenAI protocol conversion tests."""

from __future__ import annotations

import json

import pytest

from core.errors import (
    AuthenticationError,
    InvalidRequestError,
    ModelNotFoundError,
    RateLimitError,
    TimeoutError,
)
from core.models import ChatChunk, ChatResponse, Usage
from protocol.common import DONE_SSE
from protocol.openai import (
    openai_error_response,
    parse_openai_chat_request,
    to_openai_chat_response,
    to_openai_chunk_sse,
)


def test_parse_valid_request():
    req = parse_openai_chat_request(
        {
            "model": "gemini-3.8-flash",
            "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}],
            "stream": True,
            "temperature": 0.7,
        },
    )
    assert req.model == "gemini-3.8-flash"
    assert len(req.messages) == 2
    assert req.stream is True
    assert req.temperature == 0.7


def test_parse_missing_model_rejected():
    with pytest.raises(InvalidRequestError):
        parse_openai_chat_request({"messages": [{"role": "user"}]})


def test_parse_missing_messages_rejected():
    with pytest.raises(InvalidRequestError):
        parse_openai_chat_request({"model": "m"})


def test_response_serialization():
    resp = ChatResponse(
        id="chatcmpl-1",
        model="m",
        text="hello",
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )
    body = to_openai_chat_response(resp)
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hello"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] == 2


def test_chunk_sse_format():
    chunk = ChatChunk(id="chatcmpl-1", model="m", text="hi ")
    event = to_openai_chunk_sse(chunk)
    assert event.startswith("data: {")
    assert event.endswith(chr(10) + chr(10))
    payload = json.loads(event[len("data: "):].strip())
    assert payload["object"] == "chat.completion.chunk"
    assert payload["choices"][0]["delta"]["content"] == "hi "


def test_done_sse():
    assert DONE_SSE == "data: [DONE]" + chr(10) + chr(10)


def test_error_mapping_statuses():
    assert openai_error_response(RateLimitError("x"))[0] == 429
    assert openai_error_response(AuthenticationError("x"))[0] == 401
    assert openai_error_response(ModelNotFoundError("x"))[0] == 404
    assert openai_error_response(TimeoutError("x"))[0] == 504


def test_error_body_shape():
    status, body = openai_error_response(RateLimitError("too many", retry_after=3.0))
    assert status == 429
    assert body["error"]["type"] == "RateLimitError"
    assert body["error"]["code"] == 429
    assert "too many" in body["error"]["message"]
