"""Convert Code Assist response envelopes into internal Chat models.

Non-streaming body:  {"response": {...Gemini...}, "traceId": "..."}
Streaming data:      {"response": {...chunk...}, "traceId": "..."}

traceId is deliberately NOT surfaced into content/usage.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from core.models import ChatChunk, ChatResponse, Usage
from protocol.common import new_id, stable_tool_call_id


_FINISH_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
}


def unwrap_envelope(data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the inner Gemini response from the Code Assist envelope."""
    response = data.get("response")
    if isinstance(response, dict):
        return response
    return data


def extract_trace_id(data: Dict[str, Any]) -> Optional[str]:
    value = data.get("traceId")
    return str(value) if value is not None else None


def parse_response(
    data: Dict[str, Any],
    model: str,
) -> ChatResponse:
    """Parse a non-streaming Code Assist generateContent response."""
    inner = unwrap_envelope(data)
    usage_meta = inner.get("usageMetadata", {})
    candidates = inner.get("candidates", [])
    if not candidates:
        return ChatResponse(
            id=new_id(),
            model=model,
            text="",
            finish_reason="stop",
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
    data: Dict[str, Any],
    model: str,
) -> Optional[ChatChunk]:
    """Parse one streaming event. Returns None when there is no usable delta."""
    inner = unwrap_envelope(data)
    candidates = inner.get("candidates", [])
    usage_meta = inner.get("usageMetadata", {})

    if not candidates:
        usage = _parse_usage(usage_meta)
        if usage is None:
            return None
        return ChatChunk(
            id=new_id(),
            model=model,
            usage=usage,
        )

    cand = candidates[0]
    content = cand.get("content", {})
    parts = content.get("parts", [])
    text, tool_calls = _parts_to_text_and_tools(parts, streaming=True)
    fr = _map_finish_reason(cand.get("finishReason"))
    if tool_calls and fr is not None:
        fr = "tool_calls"
    usage = _parse_usage(usage_meta)

    if text is None and not tool_calls and fr is None and usage is None:
        return None
    return ChatChunk(
        id=new_id(),
        model=model,
        text=text,
        tool_calls=tool_calls if tool_calls else None,
        finish_reason=fr,
        usage=usage,
    )


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------


def _parts_to_text_and_tools(
    parts: List[Dict[str, Any]],
    *,
    streaming: bool = False,
) -> Tuple[Optional[str], Optional[List[Dict[str, Any]]]]:
    """Separate Gemini parts into text and OpenAI-style tool_calls."""
    text_pieces: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    function_ordinal = 0
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("thought"):
            # thinking content is not surfaced to OpenAI
            continue
        if "text" in part:
            text_pieces.append(part["text"] or "")
        fc = part.get("functionCall")
        if isinstance(fc, dict):
            fn_name = fc.get("name", "")
            ordinal = function_ordinal
            function_ordinal += 1
            args = fc.get("args")
            if isinstance(args, bytes):
                try:
                    args = json.loads(args.decode("utf-8"))
                except Exception:  # noqa: BLE001
                    args = {}
            if isinstance(args, str):
                arguments = args
            else:
                if not isinstance(args, dict):
                    args = {"value": args}
                arguments = json.dumps(args, ensure_ascii=False)
            tool_calls.append(
                {
                    "id": stable_tool_call_id(ordinal, fn_name),
                    "type": "function",
                    "function": {"name": fn_name, "arguments": arguments},
                }
            )
    text = "".join(text_pieces) if text_pieces else ("" if parts else None)
    return text, tool_calls or None


def _map_finish_reason(raw: Any) -> Optional[str]:
    if not raw:
        return None
    return _FINISH_MAP.get(str(raw).upper())


def _parse_usage(meta: Any) -> Optional[Usage]:
    if not isinstance(meta, dict) or not meta:
        return None
    prompt = int(meta.get("promptTokenCount", 0) or 0)
    completion = int(meta.get("candidatesTokenCount", 0) or 0) + int(
        meta.get("thoughtsTokenCount", 0) or 0
    )
    total = int(meta.get("totalTokenCount", 0) or 0)
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total or (prompt + completion),
    )
