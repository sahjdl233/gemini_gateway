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

    Identity is maintained per tool-call ORDINAL, never by function name:
    the first time a call is seen it is assigned its own ordinal and id,
    and that ordinal keeps the same id across chunks.  Different ordinals
    always get different ids, even when the function name is identical.

    Gemini frames re-present already-emitted calls (and may omit others)
    without any stable wire id, so a repeated part is matched back to its
    ordinal by its arguments, scoped to the same function name and scanned
    in ordinal order: the first same-name call whose accumulated arguments
    are a prefix of the incoming arguments (a strict cumulative
    continuation, or an exact re-presentation which yields an empty delta)
    wins.  When no argument signal exists — e.g. a non-prefix argument
    fragment, which cannot be attributed to a specific same-name ordinal —
    the part continues the FIRST same-name call, preserving the documented
    fragment pass-through behaviour.  A function name never seen before
    starts a new ordinal.

    Some Gemini-compatible endpoints emit cumulative argument text while
    others emit fragments.  Prefix subtraction handles the cumulative form;
    non-prefix fragments are passed through unchanged.  ``state`` is scoped
    to one upstream stream.
    """
    result: list[dict[str, Any]] = []
    index_by_id = state.setdefault("__index_by_id__", {})
    tracked = state.setdefault("__tracked__", [])
    next_index = state.setdefault("__next_index__", 0)
    frame_new: set[str] = set()
    for call in tool_calls:
        item = copy.deepcopy(call)
        function = item.get("function") or {}
        name = str(function.get("name", ""))
        current = str(function.get("arguments", ""))

        call_id = None
        for entry in tracked:  # ordinal order, previous frames only
            if entry["id"] in frame_new:
                continue  # same-frame parts are always DISTINCT calls
            if entry["name"] != name:
                continue  # matching is scoped to the same function name
            accumulated = str(state.get(entry["id"], ""))
            if not accumulated:
                continue  # an empty-args call cannot disambiguate anything
            if current.startswith(accumulated):
                call_id = entry["id"]  # continuation or re-presentation
                break
        if call_id is None:
            # No argument signal (e.g. a non-prefix fragment): continue the
            # first same-name call from a previous frame rather than
            # forking identity.
            for entry in tracked:
                if entry["id"] in frame_new:
                    continue
                if entry["name"] == name:
                    call_id = entry["id"]
                    break
        if call_id is None:
            # Function name never seen before (or a second same-name part
            # within this frame): a new ordinal.
            call_id = stable_tool_call_id(next_index, name)
            tracked.append({"id": call_id, "name": name})
            frame_new.add(call_id)
        item["id"] = call_id
        if call_id not in index_by_id:
            index_by_id[call_id] = next_index
            next_index += 1
        item["index"] = index_by_id[call_id]
        previous = state.get(call_id, "")
        if previous and current.startswith(previous):
            function["arguments"] = current[len(previous):]
        state[call_id] = current if current.startswith(previous) else previous + current
        item["function"] = function
        result.append(item)
    state["__next_index__"] = next_index
    return result


DONE_SSE = "data: [DONE]\n\n"
