"""Internal (provider-agnostic) data models used across the gateway."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatMessage(BaseModel):
    role: str
    content: Optional[Any] = None  # str | list[dict] (multimodal, TASK-004)
    tool_calls: Optional[List[Any]] = None
    name: Optional[str] = None


class ChatRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tools: Optional[List[Any]] = None

    # TASK-004 (Firebase): additive optional fields so the OpenAI to Gemini
    # mapping can carry tool_choice / top_p / stop / max_completion_tokens /
    # reasoning_effort end-to-end. All default to None; existing behaviour
    # is unchanged.
    max_completion_tokens: Optional[int] = None
    top_p: Optional[float] = None
    stop: Optional[Any] = None
    tool_choice: Optional[Any] = None
    reasoning_effort: Optional[str] = None


class ChatResponse(BaseModel):
    id: str
    model: str
    text: str
    finish_reason: Optional[str] = "stop"
    tool_calls: Optional[List[Any]] = None  # TASK-004: non-streaming function calls
    usage: Optional[Usage] = None
    created: int = Field(default_factory=lambda: int(time.time()))


class ChatChunk(BaseModel):
    id: str
    model: str
    text: Optional[str] = None
    tool_calls: Optional[List[Any]] = None
    finish_reason: Optional[str] = None
    usage: Optional[Usage] = None
    created: int = Field(default_factory=lambda: int(time.time()))


class ModelInfo(BaseModel):
    id: str
    provider: str
    capabilities: Dict[str, bool] = Field(default_factory=dict)
