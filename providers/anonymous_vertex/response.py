"""Response conversion: GeminiChunk / candidates -> Gateway ChatResponse / ChatChunk.

This is the inverse of request.py.  streaming.py parses raw upstream bytes into
GeminiChunk-like structures; this module converts those internal chunks into
the Gateway chat models the Provider yields to the Scheduler / OpenAI layer.
"""

from __future__ import annotations

from typing import List, Optional

from core.models import ChatChunk, ChatResponse, Usage


def vertex_response_to_chat_response(
    candidates: list,
    usage_metadata: Optional[dict] = None,
    model_version: str = "",
    response_id: str = "",
) -> ChatResponse:
    """Assemble a non-streaming ChatResponse from collected candidates."""
    text_parts: List[str] = []
    finish_reason = "stop"
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        fr = cand.get("finishReason", "")
        if fr and fr != "FINISH_REASON_UNSPECIFIED":
            finish_reason = _map_finish_reason(fr)
        content = cand.get("content", {})
        parts = content.get("parts", []) if isinstance(content, dict) else []
        for p in parts:
            t = p.get("text", "") if isinstance(p, dict) else ""
            if t:
                text_parts.append(t)

    usage = _extract_usage(usage_metadata)
    return ChatResponse(
        id=response_id or "chatcmpl-anonymous-vertex",
        model=model_version or "unknown",
        text="".join(text_parts),
        finish_reason=finish_reason,
        usage=usage,
    )


def vertex_chunk_to_chat_chunk(
    candidates: list,
    usage_metadata: Optional[dict] = None,
    model_version: str = "",
    response_id: str = "",
) -> Optional[ChatChunk]:
    """Convert a single Gemini incremental chunk into a ChatChunk (or None)."""
    text_parts: List[str] = []
    finish_reason: Optional[str] = None
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        fr = cand.get("finishReason", "")
        if fr and fr != "FINISH_REASON_UNSPECIFIED":
            finish_reason = _map_finish_reason(fr)
        content = cand.get("content", {})
        parts = content.get("parts", []) if isinstance(content, dict) else []
        for p in parts:
            if not isinstance(p, dict):
                continue
            t = p.get("text", "")
            if t and not p.get("thought", False):
                text_parts.append(t)

    if not text_parts and finish_reason is None and usage_metadata is None:
        return None

    text = "".join(text_parts) if text_parts else None
    usage = _extract_usage(usage_metadata) if usage_metadata else None
    return ChatChunk(
        id=response_id or "chatcmpl-anonymous-vertex",
        model=model_version or "",
        text=text,
        finish_reason=finish_reason,
        usage=usage,
    )


_FINISH_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "BLOCKLIST": "content_filter",
}


def _map_finish_reason(fr: str) -> str:
    return _FINISH_MAP.get(fr.upper().strip(), "stop")


def _extract_usage(usage_metadata: Optional[dict]) -> Usage:
    if not usage_metadata:
        return Usage()
    return Usage(
        prompt_tokens=usage_metadata.get("promptTokenCount", 0),
        completion_tokens=usage_metadata.get("candidatesTokenCount", 0),
        total_tokens=usage_metadata.get("totalTokenCount", 0),
    )

