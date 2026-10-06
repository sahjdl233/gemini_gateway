"""Shared protocol helpers (SSE formatting, ids, timestamps)."""

from __future__ import annotations

import time
import hashlib
import copy
from typing import Any, Dict


def new_id(prefix: str = "chatcmpl") -> str:
    return f"{prefix}-{int(time.time() * 1000)}"


def format_sse(data: Dict[str, Any]) -> str:
    """Serialize one server-sent event payload."""
    import json

    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def stable_tool_call_id(index: int, name: str) -> str:
    """Return a deterministic gateway identity for a Gemini function call.

    Gemini's ``functionCall`` wire object has no OpenAI call id.  The
    provider-independent identity is therefore derived from its stable
    position and function name, so non-streaming and streaming conversions
    expose the same id without putting provider fields in core models.
    """
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    return f"call_{index}_{digest}"


def tool_call_deltas(tool_calls: list[dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert parsed Gemini calls into OpenAI streaming deltas.

    Some Gemini-compatible endpoints emit cumulative argument text while
    others emit fragments.  Prefix subtraction handles the cumulative form;
    non-prefix fragments are passed through unchanged.  ``state`` is scoped
    to one upstream stream.
    """
    result: list[dict[str, Any]] = []
    index_by_id = state.setdefault("__index_by_id__", {})
    id_by_name = state.setdefault("__id_by_name__", {})
    next_index = state.setdefault("__next_index__", 0)
    for call in tool_calls:
        item = copy.deepcopy(call)
        function = item.get("function") or {}
        incoming_id = str(item.get("id", ""))
        name = str(function.get("name", ""))
        # Gemini streams may omit previously emitted functionCalls from a
        # later parts array.  Function names are unique within a tool
        # declaration set, so retain the first generated identity when the
        # parser presents the same call with a new per-chunk ordinal.
        call_id = id_by_name.setdefault(name, incoming_id)
        item["id"] = call_id
        if call_id not in index_by_id:
            index_by_id[call_id] = next_index
            next_index += 1
        item["index"] = index_by_id[call_id]
        current = str(function.get("arguments", ""))
        previous = state.get(call_id, "")
        if previous and current.startswith(previous):
            function["arguments"] = current[len(previous):]
        state[call_id] = current if current.startswith(previous) else previous + current
        item["function"] = function
        result.append(item)
    state["__next_index__"] = next_index
    return result


DONE_SSE = "data: [DONE]\n\n"
