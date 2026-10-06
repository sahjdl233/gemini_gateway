"""GCLI (Code Assist) SSE parser - deliberately separate from Firebase.

Differences vs. the existing parsers (TASK-007 §9.2):
  - SSE with `data: {json}` lines (NOT Anonymous Vertex NDJSON)
  - there is NO `data: [DONE]` terminator; the stream simply ends
  - every frame wraps the Gemini payload in {"response": {...}, "traceId": ...}

The Gateway still emits its own OpenAI `[DONE]` at the route layer; it
must never rely on the upstream sentinel.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, Optional

from providers.gemini_cli.response import parse_chunk as _parse_chunk
from protocol.common import tool_call_deltas


async def iter_sse_events(response: Any) -> AsyncIterator[Dict[str, Any]]:
    """Yield decoded JSON objects from a Code Assist SSE byte stream.

    Handles arbitrary network chunking (a single event may be split across
    many byte chunks, and one chunk may contain many events). Ignores
    non-data lines, comments and the (unexpected) [DONE] sentinel.
    """
    buf = ""
    async for raw in response.aiter_bytes():
        if not raw:
            continue
        buf += raw.decode("utf-8", "ignore")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                yield json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                continue


async def iter_chunks(
    response: Any,
    model: str,
) -> AsyncIterator[Any]:
    """Parse GCLI SSE into ChatChunk objects (envelope-aware)."""
    tool_state: dict[str, Any] = {}
    async for data in iter_sse_events(response):
        chunk = _parse_chunk(data, model)
        if chunk is not None:
            if chunk.tool_calls:
                chunk.tool_calls = tool_call_deltas(chunk.tool_calls, tool_state)
            yield chunk
