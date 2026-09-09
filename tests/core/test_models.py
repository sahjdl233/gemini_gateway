"""Internal model tests (ChatRequest / ChatChunk / ModelInfo)."""

from __future__ import annotations

from core.models import ChatChunk, ChatMessage, ChatRequest, ChatResponse, ModelInfo


def test_chat_request_defaults():
    req = ChatRequest(model="m", messages=[ChatMessage(role="user", content="hi")])
    assert req.stream is False
    assert req.temperature is None
    assert req.max_tokens is None
    assert req.tools is None


def test_chunk_defaults():
    chunk = ChatChunk(id="c1", model="m")
    assert chunk.text is None
    assert chunk.tool_calls is None
    assert chunk.finish_reason is None
    assert chunk.usage is None
    assert chunk.created > 0


def test_response_defaults():
    resp = ChatResponse(id="r1", model="m", text="hello")
    assert resp.finish_reason == "stop"
    assert resp.usage is None
    assert resp.created > 0


def test_model_info_capabilities():
    info = ModelInfo(
        id="gemini-3.8-flash",
        provider="fake",
        capabilities={"stream": True, "vision": True, "tools": True},
    )
    assert info.capabilities["stream"] is True
