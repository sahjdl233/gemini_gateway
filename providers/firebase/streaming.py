"""SSE streaming parser for Firebase AI Logic upstream.

Unlike Anonymous Vertex (NDJSON brace-counting), Firebase upstream
uses standard SSE with `alt=sse`. Each line starts with `data:` and
contains a JSON Gemini chunk. The stream ends with `data: [DONE]`.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Optional

from providers.firebase.response import parse_chunk as _parse_chunk


async def iter_sse_events(
    response: Any,
) -> AsyncIterator[Dict[str, Any]]:
    """Parse SSE lines from an httpx streaming response, yield JSON dicts."""
    buf = ""
    async for raw in response.aiter_bytes():
        buf += raw.decode("utf-8", "ignore")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue


async def iter_chunks(
    response: Any, model: str
) -> AsyncIterator[Any]:
    """Parse SSE into ChatChunk objects using response.parse_chunk."""
    async for data in iter_sse_events(response):
        chunk = _parse_chunk(data, model)
        if chunk is not None:
            yield chunk
