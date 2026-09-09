"""Shared protocol helpers (SSE formatting, ids, timestamps)."""

from __future__ import annotations

import time
from typing import Any, Dict


def new_id(prefix: str = "chatcmpl") -> str:
    return f"{prefix}-{int(time.time() * 1000)}"


def format_sse(data: Dict[str, Any]) -> str:
    """Serialize one server-sent event payload."""
    import json

    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


DONE_SSE = "data: [DONE]\n\n"
