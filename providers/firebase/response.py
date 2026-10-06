"""Convert Gemini generateContent response → internal models.

TASK-003 confirmed mapping:
- candidates[0].content.parts[].text → text
- candidates[0].content.parts[].functionCall → tool_calls
- candidates[0].finishReason → finish_reason
- usageMetadata → Usage
- thought/thoughtSignature: stripped (not exposed to OpenAI)
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from core.models import ChatChunk, ChatResponse, Usage
from protocol.common import new_id, stable_tool_call_id


_FINISH_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
}


def parse_response(
    data: Dict[str, Any], model: str
) -> ChatResponse:
    """Parse a non-streaming Gemini generateContent response."""
    usage_meta = data.get("usageMetadata", {})
    candidates = data.get("candidates", [])
    if not candidates:
        return ChatResponse(
            id=new_id(), model=model, text="", finish_reason="stop",
            usage=_parse_usage(usage_meta),
        )
    cand = candidates[0]
    content = cand.get("content", {})
    parts = content.get("parts", [])
    text, tool_calls = _parts_to_text_and_tools(parts, streaming=False)
    fr = _map_finish_reason(cand.get("finishReason")) or "stop"
    if tool_calls:
        fr = "tool_calls"
    return ChatResponse(
        id=new_id(),
        model=model,
        text=text or "",
        finish_reason=fr,
        tool_calls=tool_calls if tool_calls else None,
        usage=_parse_usage(usage_meta),
    )


def parse_chunk(
    data: Dict[str, Any], model: str
) -> Optional[ChatChunk]:
    """Parse one streaming chunk. Returns None if no usable content."""
    candidates = data.get("candidates", [])
    if not candidates:
        usage_meta = data.get("usageMetadata")
        if usage_meta:
            return ChatChunk(
                id=new_id(), model=model, text=None,
                finish_reason=None, usage=_parse_usage(usage_meta),
            )
        return None
    cand = candidates[0]
    content = cand.get("content", {})
    parts = content.get("parts", [])
    text, tool_calls = _parts_to_text_and_tools(parts, streaming=True)
    fr = _map_finish_reason(cand.get("finishReason"))
    if tool_calls and fr is not None:
        fr = "tool_calls"
    usage_meta = data.get("usageMetadata")
    usage = _parse_usage(usage_meta) if usage_meta else None
    return ChatChunk(
        id=new_id(),
        model=model,
        text=text if text else None,
        tool_calls=tool_calls if tool_calls else None,
        finish_reason=fr,
        usage=usage,
    )


def _parts_to_text_and_tools(
    parts: List[Dict[str, Any]],
    *,
    streaming: bool = False,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract text and tool_calls from Gemini parts, ignoring thought."""
    text = ""
    tool_calls: List[Dict[str, Any]] = []
    for index, p in enumerate(parts):
        if "thought" in p:
            continue  # skip thinking content
        elif "text" in p:
            text += str(p["text"])
        elif "functionCall" in p:
            fc = p["functionCall"]
            name = fc.get("name", "")
            args = fc.get("args", {})
            if isinstance(args, str):
                arguments = args
            else:
                if not isinstance(args, dict):
                    args = {}
                arguments = json.dumps(args, ensure_ascii=False)
            tool_calls.append({
                "id": stable_tool_call_id(index, name),
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments,
                },
            })
    return text, tool_calls


def _map_finish_reason(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return _FINISH_MAP.get(raw.upper(), "stop")


def _parse_usage(meta: Optional[Dict[str, Any]]) -> Optional[Usage]:
    if not meta:
        return None
    return Usage(
        prompt_tokens=int(meta.get("promptTokenCount", 0)),
        completion_tokens=int(meta.get("candidatesTokenCount", 0))
        + int(meta.get("thoughtsTokenCount", 0)),
        total_tokens=int(meta.get("totalTokenCount", 0)),
    )
