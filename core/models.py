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
    content: Optional[str] = None
    tool_calls: Optional[List[Any]] = None
    name: Optional[str] = None


class ChatRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tools: Optional[List[Any]] = None


class ChatResponse(BaseModel):
    id: str
    model: str
    text: str
    finish_reason: Optional[str] = "stop"
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
