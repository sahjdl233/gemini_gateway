"""Gemini protocol mapping (reserved for later tasks).

TASK-000 only defines the conversion direction: Gemini HTTP responses ->
internal ChatChunk / ChatResponse.  No real Google API is contacted in
TASK-000, so this module only declares the seam.
"""

from __future__ import annotations

from typing import Any, Dict

from core.models import ChatChunk, ChatResponse

__all__ = ["to_internal_response", "to_internal_chunk"]


def to_internal_response(raw: Dict[str, Any]) -> ChatResponse:
    """Convert a Gemini generateContent response into a ChatResponse."""
    raise NotImplementedError("Gemini protocol conversion is reserved for a later task")


def to_internal_chunk(raw: Dict[str, Any]) -> ChatChunk:
    """Convert one Gemini streamGenerateContent chunk into a ChatChunk."""
    raise NotImplementedError("Gemini protocol conversion is reserved for a later task")
