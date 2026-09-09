"""SSE streaming helpers shared by providers.

Providers emit internal ChatChunk objects; this module only provides
small parsing helpers used by future adapters that read raw upstream SSE.
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Dict


def parse_sse_line(line: str) -> Dict[str, object]:
    """Parse one 'data: {...}' SSE line into a dict (raises ProtocolError)."""
    if not line.startswith("data:"):
        raise ValueError("expected a 'data:' SSE line")
    payload = line[len("data:"):].strip()
    if payload == "[DONE]":
        return {}
    return json.loads(payload)


async def sse_events(lines: AsyncIterator[str]) -> AsyncIterator[Dict[str, object]]:
    """Yield parsed JSON events from an async iterator of SSE text lines."""
    async for line in lines:
        if not line.strip():
            continue
        event = parse_sse_line(line)
        if event:
            yield event
