from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Mapping, Optional

from core.errors import ProviderError
from core.health import HealthResult, HealthState
from core.model_registry import ModelInfo
from core.models import ChatChunk, ChatRequest, ChatResponse, Usage
from core.provider import Provider
from execution.base import ExecutionBackend
from execution.http import HttpExecutionBackend
from protocol.common import new_id
from transport.proxy import ProxyConfig

from .client import AntigravityClient
from .model_discovery import ModelDiscovery
from .resource import AntigravityResource

logger = logging.getLogger(__name__)


def _require_resource(resource: Any) -> AntigravityResource:
    if not isinstance(resource, AntigravityResource):
        raise ProviderError(
            f"AntigravityProvider expected AntigravityResource, got {type(resource).__name__}",
            provider="antigravity",
        )
    return resource


def _convert_tools(tools: list[Any]) -> list[dict[str, Any]]:
    """Convert OpenAI function tools to Cloud Code function declarations.

    ``ChatRequest.tools`` currently carries the OpenAI function-tool shape.
    Cloud Code's GenerateContentRequest expects the declarations grouped under
    ``functionDeclarations``; unsupported tool kinds are outside the current
    Core contract and are omitted rather than sent in an incompatible shape.
    """
    declarations: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        function = tool.get("function") or {}
        if not isinstance(function, dict):
            continue
        declaration: dict[str, Any] = {
            "name": function.get("name", ""),
            "description": function.get("description", ""),
            "parameters": function.get("parameters") or {"type": "object"},
        }
        declarations.append(declaration)
    return declarations


class AntigravityProvider(Provider):
    """Antigravity adapter for OpenAI-compatible /v1/chat/completions.

    The provider owns exactly ONE HttpExecutionBackend, which owns ONE
    persistent AsyncClient shared by every AntigravityResource.
    """

    def __init__(
        self,
        client: AntigravityClient | None = None,
        backend: Optional[HttpExecutionBackend] = None,
        *,
        timeout_seconds: float = 30.0,
        proxy: Optional[ProxyConfig] = None,
        base_headers: Optional[Mapping[str, str]] = None,
        max_connections: int = 20,
        max_keepalive_connections: int = 8,
    ) -> None:
        self.backend = backend or HttpExecutionBackend(
            timeout_seconds=timeout_seconds,
            proxy=proxy,
            base_headers=base_headers,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )
        self.client = client or AntigravityClient(backend=self.backend)
        self.discovery = ModelDiscovery(self.client)
        self._last_resource: Optional[AntigravityResource] = None

    async def list_models(self) -> list[ModelInfo]:
        # Cache the last resource used for discovery to avoid repeated upstream calls
        if self._last_resource is not None:
            return await self.discovery.fetch_models(self._last_resource)
        # If no resource cached yet, return empty list (models will be loaded
        # on first complete/stream call via _update_cached_resource)
        return []

    async def _update_cached_resource(self, resource: AntigravityResource) -> None:
        self._last_resource = resource

    async def close(self) -> None:
        await self.backend.close()

    def _build_payload(self, request: ChatRequest, resource: AntigravityResource) -> dict[str, Any]:
        """Build the Antigravity Cloud Code envelope payload."""
        contents = []
        system_instruction = None

        for msg in request.messages:
            if msg.role == "system":
                system_instruction = msg.content
                continue
            role = "user" if msg.role in ("user", "human") else "model"
            content = msg.content
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, str):
                        parts.append({"text": item})
                    elif isinstance(item, dict):
                        parts.append(item)
                content_obj = {"role": role, "parts": parts}
            else:
                content_obj = {"role": role, "parts": [{"text": str(content) if content else ""}]}
            contents.append(content_obj)

        request_body: dict[str, Any] = {"contents": contents}

        if system_instruction:
            request_body["systemInstruction"] = {
                "parts": [{"text": system_instruction}]
            }

        generation_config: dict[str, Any] = {}
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        if request.top_p is not None:
            generation_config["topP"] = request.top_p
        if request.max_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_tokens
        elif request.max_completion_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_completion_tokens

        if generation_config:
            request_body["generationConfig"] = generation_config

        if request.tools:
            declarations = _convert_tools(request.tools)
            if declarations:
                request_body["tools"] = [{"functionDeclarations": declarations}]

        envelope = {
            "project": resource.project_id or "default",
            "request": request_body,
            "model": request.model,
            "userAgent": "antigravity",
            "requestType": "agent",
            "requestId": new_id(),
        }
        return envelope

    @staticmethod
    def _parse_response(data: dict[str, Any], model: str) -> ChatResponse:
        """Parse Cloud Code generateContent response."""
        inner = data.get("response", data)
        if not isinstance(inner, dict):
            inner = {}

        usage_meta = inner.get("usageMetadata", {})
        candidates = inner.get("candidates", [])

        text_parts = []
        finish_reason = "stop"

        if candidates:
            cand = candidates[0]
            content = cand.get("content", {})
            parts = content.get("parts", [])
            for part in parts:
                if "text" in part:
                    text_parts.append(part["text"])
            fr = cand.get("finishReason", "")
            if fr:
                fr_map = {
                    "STOP": "stop",
                    "MAX_TOKENS": "length",
                    "SAFETY": "content_filter",
                    "RECITATION": "content_filter",
                }
                finish_reason = fr_map.get(fr, "stop")
        else:
            # Non-candidates response - might be direct content
            content = inner.get("content", {})
            if isinstance(content, dict):
                for part in content.get("parts", []):
                    if "text" in part:
                        text_parts.append(part["text"])

        return ChatResponse(
            id=new_id(),
            model=model,
            text="".join(text_parts),
            finish_reason=finish_reason,
            usage=_parse_usage(usage_meta),
        )

    @staticmethod
    def _parse_chunk(data: dict[str, Any], model: str) -> ChatChunk:
        """Parse a single SSE event chunk."""
        inner = data.get("response", data)
        if not isinstance(inner, dict):
            inner = {}

        usage_meta = inner.get("usageMetadata", {})
        candidates = inner.get("candidates", [])

        text_parts = []
        finish_reason = None

        if candidates:
            cand = candidates[0]
            content = cand.get("content", {})
            parts = content.get("parts", [])
            for part in parts:
                if "text" in part:
                    text_parts.append(part["text"])
            fr = cand.get("finishReason")
            if fr:
                fr_map = {
                    "STOP": "stop",
                    "MAX_TOKENS": "length",
                    "SAFETY": "content_filter",
                    "RECITATION": "content_filter",
                }
                finish_reason = fr_map.get(fr, "stop")
        else:
            content = inner.get("content", {})
            if isinstance(content, dict):
                for part in content.get("parts", []):
                    if "text" in part:
                        text_parts.append(part["text"])

        return ChatChunk(
            id=new_id(),
            model=model,
            text="".join(text_parts) if text_parts else None,
            finish_reason=finish_reason,
            usage=_parse_usage(usage_meta) if usage_meta else None,
        )

    async def complete(self, request: ChatRequest, resource: Any) -> ChatResponse:
        ag_res = _require_resource(resource)
        await self._update_cached_resource(ag_res)

        payload = self._build_payload(request, ag_res)
        data = await self.client.generate_content(ag_res, payload)

        return self._parse_response(data, request.model)

    async def stream(
        self, request: ChatRequest, resource: Any
    ) -> AsyncIterator[ChatChunk]:
        ag_res = _require_resource(resource)
        await self._update_cached_resource(ag_res)

        payload = self._build_payload(request, ag_res)
        resp = await self.client.stream_generate_content(ag_res, payload)

        buf = ""
        async for raw in resp.aiter_bytes():
            if not raw:
                continue
            buf += raw.decode("utf-8", "ignore")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                payload_str = line[len("data:"):].strip()
                if not payload_str or payload_str == "[DONE]":
                    continue
                try:
                    data = json.loads(payload_str)
                    chunk = self._parse_chunk(data, request.model)
                    if chunk.text or chunk.finish_reason or chunk.usage:
                        yield chunk
                except (json.JSONDecodeError, ValueError):
                    continue

    async def health_check(self, resource: Any) -> HealthResult:
        ag_res = _require_resource(resource)
        try:
            await self.client.fetch_available_models(ag_res)
            return HealthResult(state=HealthState.HEALTHY)
        except Exception as e:
            return HealthResult(state=HealthState.UNHEALTHY, reason=str(e))


def _parse_usage(usage_meta: dict[str, Any]) -> Optional[Usage]:
    if not usage_meta:
        return None
    pass
    return Usage(
        prompt_tokens=usage_meta.get("promptTokenCount", 0),
        completion_tokens=usage_meta.get("candidatesTokenCount", 0),
        total_tokens=usage_meta.get("totalTokenCount", 0),
    )




