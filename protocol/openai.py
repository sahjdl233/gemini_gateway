"""OpenAI-compatible protocol handlers.

parse_openai_chat_request: OpenAI JSON -> internal ChatRequest
to_openai_chat_response:   internal ChatResponse -> OpenAI JSON
to_openai_chunk_sse:       internal ChatChunk -> OpenAI SSE payload
openai_error_response:     ProviderError -> {status, openai error body}
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from core.errors import (
    InvalidRequestError,
    ProviderError,
    provider_error_status,
)
from core.models import ChatMessage, ChatRequest, ChatResponse, ChatChunk

from .common import DONE_SSE, format_sse, new_id


def parse_openai_chat_request(payload: Dict[str, Any]) -> ChatRequest:
    """Translate an OpenAI /v1/chat/completions body into ChatRequest."""
    if not isinstance(payload, dict):
        raise InvalidRequestError("request body must be a JSON object", provider="gateway")
    model = payload.get("model")
    if not model or not isinstance(model, str):
        raise InvalidRequestError("missing required field 'model'", provider="gateway")
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise InvalidRequestError("missing required field 'messages'", provider="gateway")
    messages: list = []
    for item in raw_messages:
        if not isinstance(item, dict) or "role" not in item:
            raise InvalidRequestError("each message must have a 'role'", provider="gateway")
        messages.append(
            ChatMessage(
                role=item["role"],
                content=item.get("content"),
                tool_calls=item.get("tool_calls"),
                name=item.get("name"),
            )
        )
    return ChatRequest(
        model=model,
        messages=messages,
        stream=bool(payload.get("stream", False)),
        temperature=payload.get("temperature"),
        max_tokens=payload.get("max_tokens"),
        tools=payload.get("tools"),
        # TASK-004 (Firebase): pass through OpenAI fields that the
        # FirebasePayloadBuilder maps into Gemini generationConfig / toolConfig.
        max_completion_tokens=payload.get("max_completion_tokens"),
        top_p=payload.get("top_p"),
        stop=payload.get("stop"),
        tool_choice=payload.get("tool_choice"),
        reasoning_effort=payload.get("reasoning_effort"),
    )


def to_openai_chat_response(response: ChatResponse) -> Dict[str, Any]:
    """Serialize an internal ChatResponse into OpenAI JSON."""
    usage = None
    if response.usage:
        usage = response.usage.model_dump()
    data = {
        "id": response.id,
        "object": "chat.completion",
        "created": response.created,
        "model": response.model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response.text,
                },
                "finish_reason": response.finish_reason or "stop",
            }
        ],
        "usage": usage,
    }
    if response.tool_calls:
        data["choices"][0]["message"]["tool_calls"] = response.tool_calls
    return data


def to_openai_chunk_sse(chunk: ChatChunk) -> str:
    """Serialize an internal ChatChunk into one OpenAI SSE message."""
    choice: Dict[str, Any] = {
        "index": 0,
        "delta": {},
        "finish_reason": None,
    }
    delta: Dict[str, Any] = {}
    if chunk.text is not None:
        delta["content"] = chunk.text
    if chunk.tool_calls is not None:
        delta["tool_calls"] = chunk.tool_calls
    if delta:
        delta.setdefault("role", "assistant")
    choice["delta"] = delta
    if chunk.finish_reason is not None:
        choice["finish_reason"] = chunk.finish_reason
    payload = {
        "id": chunk.id,
        "object": "chat.completion.chunk",
        "created": chunk.created,
        "model": chunk.model,
        "choices": [choice],
    }
    if chunk.usage is not None:
        payload["usage"] = chunk.usage.model_dump()
    return format_sse(payload)


def openai_error_response(error: ProviderError) -> Tuple[int, Dict[str, Any]]:
    """Map a ProviderError to (HTTP status, OpenAI-style error body)."""
    status = provider_error_status(error)
    message = str(error) or error.__class__.__name__
    body = {
        "error": {
            "message": message,
            "type": error.__class__.__name__,
            "code": status,
        }
    }
    return status, body
