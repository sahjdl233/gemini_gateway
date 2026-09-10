"""Request conversion: Gateway ChatRequest -> AnonymousVertexRequest.

This is the single place that maps an OpenAI/Gateway request into the
internal AnonymousVertexRequest model.  The Provider never builds Google
payloads; it calls chat_to_vertex_request() and hands the result to the
Client/Protocol layer.

Conversion flow::

    OpenAI / SillyTavern request (ChatRequest)
              |  request.chat_to_vertex_request()
              v
    AnonymousVertexRequest (internal model)
              |  protocol.build_graphql_payload()
              v
    GraphQL variables (Google)

Only fields that really exist upstream (see docs/anonymous-vertex-protocol.md
and vertex-singbox dto.go) are modelled.  OpenAI-only concepts (tool arrays,
roles) are converted here and never placed raw into Google variables.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.models import ChatMessage, ChatRequest

from providers.anonymous_vertex.models import (
    AnonymousVertexContent,
    AnonymousVertexGenerationConfig,
    AnonymousVertexPart,
    AnonymousVertexRequest,
)
from providers.anonymous_vertex.signature import trim_gemini_path_prefix

# Model specs (from vertex-singbox config/models.json; text family only).
# max_output is the upstream generationConfig.maxOutputTokens default / cap.
TEXT_MODEL_SPECS: Dict[str, dict] = {
    "gemini-2.5-flash":       {"max_output": 65535, "default_temp": 1.0, "default_top_p": 1.0},
    "gemini-2.5-flash-lite":  {"max_output": 65535, "default_temp": 1.0, "default_top_p": 0.95},
    "gemini-2.5-pro":         {"max_output": 65535, "default_temp": 1.0, "default_top_p": 0.95},
    "gemini-3-flash-preview": {"max_output": 65535, "default_temp": None, "default_top_p": None},
    "gemini-3.1-flash-lite":  {"max_output": 65535, "default_temp": 1.0, "default_top_p": 0.95},
    "gemini-3.1-pro-preview": {"max_output": 65535, "default_temp": 1.0, "default_top_p": 0.95},
    "gemini-3.5-flash":       {"max_output": 65535, "default_temp": 1.0, "default_top_p": 0.95},
    "gemini-3.5-flash-lite":  {"max_output": 65535, "default_temp": 1.0, "default_top_p": 0.95},
    "gemini-3.6-flash":       {"max_output": 65535, "default_temp": None, "default_top_p": None},
    "gemini-3.7-flash":       {"max_output": 65535, "default_temp": None, "default_top_p": None},
}

DEFAULT_SAFETY_SETTINGS: List[Dict[str, str]] = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]


def get_model_spec(model: str) -> dict:
    """Return the model spec for a model id (fallback to a generic spec)."""
    norm = trim_gemini_path_prefix(model).lower().strip()
    return TEXT_MODEL_SPECS.get(norm, {"max_output": 65535, "default_temp": None, "default_top_p": None})


def chat_messages_to_contents(messages: List[ChatMessage]) -> List[dict]:
    """Map gateway messages to raw contents dicts (pre-normalization)."""
    contents: List[dict] = []
    for msg in messages:
        role = msg.role.lower().strip()
        if role == "system":
            continue
        if role in ("assistant",):
            role = "model"
        parts: List[dict] = []
        if msg.content:
            parts.append({"text": msg.content})
        if parts:
            contents.append({"role": role, "parts": parts})
    return contents


# -- content normalization (mirrors transform request_variables.go) --


def sanitize_contents_role(contents: List[dict]) -> List[dict]:
    """Ensure every content entry has a non-empty role (default user)."""
    for c in contents:
        role = (c.get("role") or "").strip()
        if not role:
            c["role"] = "user"
    return contents


def merge_contiguous_roles(contents: List[dict]) -> List[dict]:
    """Merge adjacent same-role contents (except functionResponse turns)."""
    if not contents:
        return contents
    merged: List[dict] = []
    for c in contents:
        parts = c.get("parts", [])
        has_fr = any(p.get("functionResponse") is not None for p in parts)
        if not merged:
            merged.append(c)
            continue
        prev = merged[-1]
        prev_has_fr = any(p.get("functionResponse") is not None for p in prev.get("parts", []))
        if c.get("role") == prev.get("role") and not has_fr and not prev_has_fr:
            prev["parts"] = prev.get("parts", []) + parts
        else:
            merged.append(c)
    return merged


def filter_empty_contents(contents: List[dict]) -> List[dict]:
    """Drop entries with no parts after filtering empty parts."""
    result: List[dict] = []
    for c in contents:
        parts = [p for p in c.get("parts", []) if _part_has_content(p)]
        if parts:
            result.append({**c, "parts": parts})
    return result


def _part_has_content(p: dict) -> bool:
    """Check whether a raw part carries meaningful content."""
    return bool(
        p.get("text")
        or p.get("thought")
        or p.get("functionCall")
        or p.get("functionResponse")
        or p.get("inlineData")
        or p.get("fileData")
        or p.get("executableCode")
        or p.get("codeExecutionResult")
        or p.get("thoughtSignature")
    )


def _content_from_dict(c: dict) -> AnonymousVertexContent:
    """Build an AnonymousVertexContent from a raw contents dict."""
    parts: List[AnonymousVertexPart] = []
    for p in c.get("parts", []) or []:
        if not isinstance(p, dict):
            continue
        parts.append(
            AnonymousVertexPart(
                text=p.get("text", ""),
                thought=bool(p.get("thought", False)),
                thought_signature=p.get("thoughtSignature"),
                inline_data=p.get("inlineData"),
                file_data=p.get("fileData"),
                function_call=p.get("functionCall"),
                function_response=p.get("functionResponse"),
                executable_code=p.get("executableCode"),
                code_execution_result=p.get("codeExecutionResult"),
                video_metadata=p.get("videoMetadata"),
                media_resolution=p.get("mediaResolution", ""),
            )
        )
    return AnonymousVertexContent(role=str(c.get("role", "user")), parts=parts)


def chat_to_vertex_request(request: ChatRequest) -> AnonymousVertexRequest:
    """Convert a Gateway ChatRequest into the internal AnonymousVertexRequest.

    The returned model carries only fields that exist upstream; protocol
    fields (model, region, recaptchaToken) are added by protocol.py.
    """
    spec = get_model_spec(request.model)
    contents = chat_messages_to_contents(request.messages)
    contents = sanitize_contents_role(contents)
    contents = merge_contiguous_roles(contents)
    contents = filter_empty_contents(contents)
    model_contents = [_content_from_dict(c) for c in contents]

    system_instruction: Optional[AnonymousVertexContent] = None
    for msg in request.messages:
        if msg.role.lower().strip() == "system" and msg.content:
            system_instruction = AnonymousVertexContent(
                role="user",
                parts=[AnonymousVertexPart(text=msg.content)],
            )
            break

    gen_config = AnonymousVertexGenerationConfig(
        temperature=_resolve_temperature(request, spec),
        max_output_tokens=_resolve_max_tokens(request, spec),
        top_p=spec.get("default_top_p"),
    )

    tools: List[Dict[str, Any]] = []
    if request.tools:
        tools = _convert_tools(request.tools)

    return AnonymousVertexRequest(
        model=request.model,
        contents=model_contents,
        system_instruction=system_instruction,
        safety_settings=list(DEFAULT_SAFETY_SETTINGS),
        generation_config=gen_config,
        tools=tools,
    )


def _resolve_temperature(request: ChatRequest, spec: dict) -> Optional[float]:
    if request.temperature is not None:
        return max(0.0, min(2.0, float(request.temperature)))
    return spec.get("default_temp")


def _resolve_max_tokens(request: ChatRequest, spec: dict) -> int:
    cap = int(spec.get("max_output", 65535))
    if request.max_tokens is not None:
        return min(cap, int(request.max_tokens))
    return cap


# -- native tool schema conversion (private GraphQL endpoint Schema) --


def _convert_tools(tools: List[Any]) -> List[Dict[str, Any]]:
    """Convert OpenAI tool list into upstream tools (functionDeclarations)."""
    declarations: List[Dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        func = tool.get("function")
        if not func or not isinstance(func, dict):
            continue
        decl: Dict[str, Any] = {"name": func.get("name", "")}
        if func.get("description"):
            decl["description"] = func["description"]
        if func.get("parameters"):
            decl["parameters"] = _convert_schema(func["parameters"])
        declarations.append(decl)
    if not declarations:
        return []
    return [{"functionDeclarations": declarations}]


def _convert_schema(schema: Any) -> Any:
    """Convert an OpenAI JSON schema into the native UI Map-style schema.

    - type is uppercased (OBJECT/STRING/NUMBER/...)
    - properties become a key/value array [{key, schema}]
    (mirrors transform prepareNativeTools / toNativeSchema)
    """
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    if "type" in out:
        out["type"] = str(out["type"]).upper()
    if "properties" in out and isinstance(out["properties"], dict):
        new_props: List[dict] = []
        for pname, pschema in out["properties"].items():
            new_props.append({"key": pname, "schema": _convert_schema(pschema)})
        out["properties"] = new_props
    for key in ("items", "anyOf", "oneOf", "allOf"):
        if key in out and isinstance(out[key], dict):
            out[key] = _convert_schema(out[key])
        elif key in out and isinstance(out[key], list):
            out[key] = [_convert_schema(s) for s in out[key]]
    return out

