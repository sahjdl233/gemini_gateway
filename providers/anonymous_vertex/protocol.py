"""Request and response conversion between Gateway internal models and Anonymous Vertex upstream."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.models import ChatChunk, ChatMessage, ChatRequest, ChatResponse, Usage

TEXT_MODEL_SPECS = {
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

DEFAULT_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]


def get_model_spec(model: str) -> dict:
    from providers.anonymous_vertex.signature import trim_gemini_path_prefix
    norm = trim_gemini_path_prefix(model).lower().strip()
    return TEXT_MODEL_SPECS.get(norm, {"max_output": 65535, "default_temp": None, "default_top_p": None})


def chat_messages_to_contents(messages: List[ChatMessage]) -> List[dict]:
    contents = []
    for msg in messages:
        role = msg.role.lower().strip()
        if role == "system":
            continue
        if role in ("assistant",):
            role = "model"
        parts = []
        if msg.content:
            parts.append({"text": msg.content})
        if parts:
            contents.append({"role": role, "parts": parts})
    return contents


def chat_to_vertex_request(request: ChatRequest) -> dict:
    from providers.anonymous_vertex.signature import (
        filter_empty_contents,
        merge_contiguous_roles,
        sanitize_contents_role,
    )
    spec = get_model_spec(request.model)
    contents = chat_messages_to_contents(request.messages)
    contents = sanitize_contents_role(contents)
    contents = merge_contiguous_roles(contents)
    contents = filter_empty_contents(contents)

    system_instruction = None
    for msg in request.messages:
        if msg.role.lower().strip() == "system" and msg.content:
            system_instruction = {"role": "user", "parts": [{"text": msg.content}]}
            break

    gen_config: Dict[str, Any] = {}

    if request.temperature is not None:
        gen_config["temperature"] = max(0.0, min(2.0, float(request.temperature)))
    elif spec.get("default_temp") is not None:
        gen_config["temperature"] = spec["default_temp"]

    if request.max_tokens is not None:
        gen_config["maxOutputTokens"] = min(spec["max_output"], int(request.max_tokens))
    else:
        gen_config["maxOutputTokens"] = spec["max_output"]

    if spec.get("default_top_p") is not None:
        gen_config["topP"] = spec["default_top_p"]

    result: Dict[str, Any] = {
        "contents": contents,
        "safetySettings": DEFAULT_SAFETY_SETTINGS,
        "generationConfig": gen_config,
    }

    if system_instruction is not None:
        result["systemInstruction"] = system_instruction

    if request.tools:
        tools = _convert_tools(request.tools)
        if tools:
            result["tools"] = tools

    return result


def _convert_tools(tools: List[Any]) -> List[dict]:
    declarations = []
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
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    if "type" in out:
        out["type"] = str(out["type"]).upper()
    if "properties" in out and isinstance(out["properties"], dict):
        new_props = []
        for pname, pschema in out["properties"].items():
            new_props.append({"key": pname, "schema": _convert_schema(pschema)})
        out["properties"] = new_props
    for key in ("items", "anyOf", "oneOf", "allOf"):
        if key in out and isinstance(out[key], dict):
            out[key] = _convert_schema(out[key])
        elif key in out and isinstance(out[key], list):
            out[key] = [_convert_schema(s) for s in out[key]]
    return out


def vertex_response_to_chat_response(
    candidates: list,
    usage_metadata: Optional[dict] = None,
    model_version: str = "",
    response_id: str = "",
) -> ChatResponse:
    text_parts = []
    finish_reason = "stop"
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        fr = cand.get("finishReason", "")
        if fr and fr != "FINISH_REASON_UNSPECIFIED":
            finish_reason = _map_finish_reason(fr)
        content = cand.get("content", {})
        parts = content.get("parts", [])
        for p in parts:
            t = p.get("text", "")
            if t:
                text_parts.append(t)

    usage = _extract_usage(usage_metadata)
    return ChatResponse(
        id=response_id or "chatcmpl-anonymous-vertex",
        model=model_version or "unknown",
        text="".join(text_parts),
        finish_reason=finish_reason,
        usage=usage,
    )


def vertex_chunk_to_chat_chunk(
    candidates: list,
    usage_metadata: Optional[dict] = None,
    model_version: str = "",
    response_id: str = "",
) -> Optional[ChatChunk]:
    text_parts: List[str] = []
    finish_reason: Optional[str] = None
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        fr = cand.get("finishReason", "")
        if fr and fr != "FINISH_REASON_UNSPECIFIED":
            finish_reason = _map_finish_reason(fr)
        content = cand.get("content", {})
        parts = content.get("parts", [])
        for p in parts:
            t = p.get("text", "")
            if t and not p.get("thought", False):
                text_parts.append(t)

    if not text_parts and finish_reason is None and usage_metadata is None:
        return None

    text = "".join(text_parts) if text_parts else None
    usage = _extract_usage(usage_metadata) if usage_metadata else None
    return ChatChunk(
        id=response_id or "chatcmpl-anonymous-vertex",
        model=model_version or "",
        text=text,
        finish_reason=finish_reason,
        usage=usage,
    )


_FINISH_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "BLOCKLIST": "content_filter",
}


def _map_finish_reason(fr: str) -> str:
    return _FINISH_MAP.get(fr.upper().strip(), "stop")


def _extract_usage(usage_metadata: Optional[dict]) -> Usage:
    if not usage_metadata:
        return Usage()
    return Usage(
        prompt_tokens=usage_metadata.get("promptTokenCount", 0),
        completion_tokens=usage_metadata.get("candidatesTokenCount", 0),
        total_tokens=usage_metadata.get("totalTokenCount", 0),
    )
