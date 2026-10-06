from __future__ import annotations

import json
import logging
import urllib.parse
from codecs import getincrementaldecoder
from typing import Any, AsyncIterator, Callable, Dict, Mapping, Optional, Sequence

from core.errors import ProtocolError, ProviderError
from core.health import HealthResult, HealthState
from core.model_registry import ModelInfo
from core.models import ChatChunk, ChatRequest, ChatResponse, Usage
from core.provider import Provider
from core.resource import Resource
from execution.base import ExecutionBackend
from execution.http import HttpExecutionBackend
from protocol.common import new_id, stable_tool_call_id, tool_call_deltas
from transport.proxy import ProxyConfig

from .auth_adapter import AntigravityAuthAdapter
from .client import AntigravityClient
from .model_discovery import ModelDiscovery
from .resource import AntigravityResource

logger = logging.getLogger(__name__)

DiscoveryResourceSource = Callable[[], Sequence[Resource]]


class _BackendHttpPost:
    """Minimal ``post(url, *, headers, data)`` surface over the provider's
    shared ExecutionBackend (AUTH-006 wiring).

    Lets the auth implementation issue its token endpoint call through the
    same pooled transport as API requests without the backend learning
    anything about OAuth.  ``data`` is a form dict, encoded here.
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    async def post(self, url: str, *, headers: Any = None, data: Any = None) -> Any:
        body = (
            urllib.parse.urlencode(data).encode("utf-8")
            if data is not None
            else None
        )
        return await self._backend.execute(
            "POST", url, headers=headers, data=body
        )


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
        credential_store: Optional[Any] = None,
    ) -> None:
        self.backend = backend or HttpExecutionBackend(
            timeout_seconds=timeout_seconds,
            proxy=proxy,
            base_headers=base_headers,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )
        # Per-resource AntigravityAuthAdapter instances (AUTH-006).  The
        # adapter owns the OAuth lifecycle (cache/refresh/rotation) and the
        # credential->material resolution; the Provider itself owns no auth
        # logic.  Cache scope is resource-scoped: one adapter/token cache
        # per Resource, even when Resources share a credential_id.
        self._adapters: Dict[str, AntigravityAuthAdapter] = {}
        self._credential_store = credential_store
        self.client = client or AntigravityClient(
            backend=self.backend,
            token_resolver=self._runtime_token_for,
        )
        self.discovery = ModelDiscovery(self.client)
        # A controlled, read-only resource view supplied by the application
        # lifecycle. The provider does not own, create, or persist resources
        # from this source.
        self._discovery_resource_source: Optional[DiscoveryResourceSource] = None
        self._last_resource: Optional[AntigravityResource] = None

    def set_credential_store(self, store: Any) -> None:
        """Attach the application-wide credential store (AUTH-002)."""
        self._credential_store = store
        self._adapters.clear()

    async def _adapter_for(self, resource: AntigravityResource) -> AntigravityAuthAdapter:
        """The resource-scoped auth adapter owning this resource's OAuth
        lifecycle.  Its auth implementation reuses the provider's shared
        ExecutionBackend for token endpoint calls via a minimal post()
        bridge — the backend itself learns nothing about OAuth."""
        adapter = self._adapters.get(resource.id)
        if adapter is None:
            adapter = AntigravityAuthAdapter(
                http=_BackendHttpPost(self.backend),
                credential_store=self._credential_store,
            )
            self._adapters[resource.id] = adapter
        return adapter

    async def _runtime_token_for(self, resource: AntigravityResource) -> Optional[str]:
        """Runtime bearer token for one request (may await a refresh)."""
        adapter = await self._adapter_for(resource)
        return await adapter.auth.get_access_token(resource)

    def set_discovery_resource_source(
        self, source: DiscoveryResourceSource
    ) -> None:
        """Attach the existing pool's read-only resource view for discovery."""
        self._discovery_resource_source = source

    def _select_discovery_resource(self) -> Optional[AntigravityResource]:
        if self._discovery_resource_source is not None:
            resources = self._discovery_resource_source()
        elif self._last_resource is not None:
            resources = [self._last_resource]
        else:
            return None

        for resource in resources:
            if isinstance(resource, AntigravityResource) and resource.enabled:
                return resource
        return None

    async def list_models(self) -> list[ModelInfo]:
        resource = self._select_discovery_resource()
        if resource is not None:
            return await self.discovery.fetch_models(resource)
        return []

    async def _update_cached_resource(self, resource: AntigravityResource) -> None:
        self._last_resource = resource

    def invalidate_resource(self, resource_id: str) -> None:
        """Drop the auth adapter (OAuth lifecycle state) of one resource.

        CONTROL-006-FIX-1: called by the management layer when a resource
        definition is replaced (credential rebind, disable, any definition
        change) or removed.  The adapter cache is keyed by ``resource.id``,
        so without this a re-bound or deleted-and-recreated resource would
        inherit the previous OAuth state — notably the rotated refresh
        token, which would then win over the new credential's material.

        Safe with respect to rotation (AUTH-014): a rotated refresh token
        is durably persisted through the rotation listener before it
        becomes runtime state, so dropping the adapter never loses valid
        refresh material — the next request rebuilds the adapter and
        resolves material afresh from the current definition.
        """
        self._adapters.pop(resource_id, None)
        if self._last_resource is not None and self._last_resource.id == resource_id:
            self._last_resource = None

    async def close(self) -> None:
        await self.backend.close()

    def _build_payload(self, request: ChatRequest, resource: AntigravityResource) -> dict[str, Any]:
        """Build the Antigravity Cloud Code envelope payload."""
        contents = []
        system_instruction = None

        call_names: dict[str, str] = {}
        pending_names: list[str] = []
        for msg in request.messages:
            if msg.role == "system":
                system_instruction = msg.content
                continue
            if msg.role == "tool":
                if msg.tool_call_id:
                    name = call_names.get(msg.tool_call_id, msg.name or "unknown")
                else:
                    name = msg.name or (pending_names.pop(0) if pending_names else "unknown")
                try:
                    result = json.loads(msg.content) if isinstance(msg.content, str) else msg.content
                except (TypeError, ValueError):
                    result = {"result": msg.content}
                contents.append({
                    "role": "user",
                    "parts": [{"functionResponse": {"name": name, "response": result}}],
                })
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
                if msg.role == "assistant":
                    for tc in (msg.tool_calls or []):
                        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                        name = fn.get("name", "")
                        call_id = tc.get("id") if isinstance(tc, dict) else None
                        if call_id:
                            call_names[str(call_id)] = name
                        else:
                            pending_names.append(name)
                        args = fn.get("arguments", "{}")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args) if args else {}
                            except (TypeError, ValueError):
                                args = {}
                        parts.append({"functionCall": {"name": name, "args": args if isinstance(args, dict) else {}}})
                content_obj = {"role": role, "parts": parts}
            elif msg.role == "assistant":
                parts = []
                if isinstance(content, str) and content:
                    parts.append({"text": content})
                for tc in (msg.tool_calls or []):
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    name = fn.get("name", "")
                    call_id = tc.get("id") if isinstance(tc, dict) else None
                    if call_id:
                        call_names[str(call_id)] = name
                    else:
                        pending_names.append(name)
                    args = fn.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args) if args else {}
                        except (TypeError, ValueError):
                            args = {}
                    parts.append({"functionCall": {"name": name, "args": args if isinstance(args, dict) else {}}})
                if not parts:
                    parts = [{"text": ""}]
                content_obj = {"role": "model", "parts": parts}
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
        tool_calls = []
        finish_reason = "stop"

        if candidates:
            cand = candidates[0]
            content = cand.get("content", {})
            parts = content.get("parts", [])
            for index, part in enumerate(parts):
                if "text" in part:
                    text_parts.append(part["text"])
                fc = part.get("functionCall")
                if isinstance(fc, dict):
                    name = fc.get("name", "")
                    args = fc.get("args", {})
                    if not isinstance(args, str):
                        args = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)
                    tool_calls.append({
                        "id": stable_tool_call_id(index, name),
                        "type": "function",
                        "function": {"name": name, "arguments": args},
                    })
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
                for index, part in enumerate(content.get("parts", [])):
                    if "text" in part:
                        text_parts.append(part["text"])
                    fc = part.get("functionCall")
                    if isinstance(fc, dict):
                        name = fc.get("name", "")
                        args = fc.get("args", {})
                        if not isinstance(args, str):
                            args = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)
                        tool_calls.append({
                            "id": stable_tool_call_id(index, name),
                            "type": "function",
                            "function": {"name": name, "arguments": args},
                        })

        return ChatResponse(
            id=new_id(),
            model=model,
            text="".join(text_parts),
            finish_reason=finish_reason,
            tool_calls=tool_calls or None,
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
        tool_calls = []
        finish_reason = None

        if candidates:
            cand = candidates[0]
            content = cand.get("content", {})
            parts = content.get("parts", [])
            for index, part in enumerate(parts):
                if "text" in part:
                    text_parts.append(part["text"])
                fc = part.get("functionCall")
                if isinstance(fc, dict):
                    name = fc.get("name", "")
                    args = fc.get("args", {})
                    if not isinstance(args, str):
                        args = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)
                    tool_calls.append({
                        "id": stable_tool_call_id(index, name),
                        "type": "function",
                        "function": {"name": name, "arguments": args},
                    })
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
                for index, part in enumerate(content.get("parts", [])):
                    if "text" in part:
                        text_parts.append(part["text"])
                    fc = part.get("functionCall")
                    if isinstance(fc, dict):
                        name = fc.get("name", "")
                        args = fc.get("args", {})
                        if not isinstance(args, str):
                            args = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)
                        tool_calls.append({
                            "id": stable_tool_call_id(index, name),
                            "type": "function",
                            "function": {"name": name, "arguments": args},
                        })

        return ChatChunk(
            id=new_id(),
            model=model,
            text="".join(text_parts) if text_parts else None,
            tool_calls=tool_calls or None,
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
        tool_state: dict[str, Any] = {}
        decoder = getincrementaldecoder("utf-8")("strict")
        try:
            async for raw in resp.aiter_bytes():
                if not raw:
                    continue
                try:
                    buf += decoder.decode(raw)
                except UnicodeDecodeError as exc:
                    raise ProtocolError(
                        "Antigravity upstream returned malformed SSE UTF-8",
                        provider="antigravity",
                        resource_id=ag_res.id,
                    ) from exc
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload_str = line[len("data:"):].strip()
                    if not payload_str:
                        continue
                    if payload_str == "[DONE]":
                        return
                    try:
                        data = json.loads(payload_str)
                    except json.JSONDecodeError as exc:
                        raise ProtocolError(
                            "Antigravity upstream returned malformed SSE JSON",
                            provider="antigravity",
                            resource_id=ag_res.id,
                        ) from exc
                    if not isinstance(data, dict):
                        raise ProtocolError(
                            "Antigravity upstream returned non-object SSE data",
                            provider="antigravity",
                            resource_id=ag_res.id,
                        )
                    chunk = self._parse_chunk(data, request.model)
                    if chunk.tool_calls:
                        chunk.tool_calls = tool_call_deltas(chunk.tool_calls, tool_state)
                    if chunk.text or chunk.tool_calls or chunk.finish_reason or chunk.usage:
                        yield chunk
            try:
                buf += decoder.decode(b"", final=True)
            except UnicodeDecodeError as exc:
                raise ProtocolError(
                    "Antigravity upstream returned incomplete SSE UTF-8",
                    provider="antigravity",
                    resource_id=ag_res.id,
                ) from exc
            # SSE permits the final event to omit its trailing blank line.
            # Parse that last event instead of silently discarding it.
            if buf.strip():
                line = buf.strip()
                if not line.startswith("data:"):
                    raise ProtocolError(
                        "Antigravity upstream returned malformed SSE event",
                        provider="antigravity",
                        resource_id=ag_res.id,
                    )
                payload_str = line[len("data:"):].strip()
                if payload_str and payload_str != "[DONE]":
                    try:
                        data = json.loads(payload_str)
                    except json.JSONDecodeError as exc:
                        raise ProtocolError(
                            "Antigravity upstream returned malformed SSE JSON",
                            provider="antigravity",
                            resource_id=ag_res.id,
                        ) from exc
                    if not isinstance(data, dict):
                        raise ProtocolError(
                            "Antigravity upstream returned non-object SSE data",
                            provider="antigravity",
                            resource_id=ag_res.id,
                        )
                    chunk = self._parse_chunk(data, request.model)
                    if chunk.tool_calls:
                        chunk.tool_calls = tool_call_deltas(chunk.tool_calls, tool_state)
                    if chunk.text or chunk.tool_calls or chunk.finish_reason or chunk.usage:
                        yield chunk
        finally:
            await resp.aclose()

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
    return Usage(
        prompt_tokens=usage_meta.get("promptTokenCount", 0),
        completion_tokens=usage_meta.get("candidatesTokenCount", 0),
        total_tokens=usage_meta.get("totalTokenCount", 0),
    )




