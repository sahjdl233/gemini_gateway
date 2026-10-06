"""Convert ChatRequest → Gemini generateContent payload.

Follows TASK-003 confirmed mapping:
- system messages → systemInstruction
- user/assistant/tool messages → contents
- tools/tool_choice → tools + toolConfig
- temperature/max_tokens/top_p/stop → generationConfig
- reasoning_effort → thinkingConfig
- multimodal: text, image data URL, audio data URL
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from core.models import ChatRequest

_THINK_LEVELS = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH"}


def build_payload(request: ChatRequest) -> Dict[str, Any]:
    """Convert a ChatRequest into a Gemini generateContent body."""
    system_text, contents = _convert_messages(request.messages)
    payload: Dict[str, Any] = {"contents": contents}
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": system_text}]}

    # tools
    if request.tools:
        gemini_tools = _convert_tools(request.tools)
        if gemini_tools:
            fcc = {"mode": "AUTO"}
            tc = getattr(request, "tool_choice", None)
            fcc = _convert_tool_choice(tc, fcc)
            payload["tools"] = [{"functionDeclarations": gemini_tools}]
            payload["toolConfig"] = {"functionCallingConfig": fcc}

    # generationConfig
    gc = _build_generation_config(request)
    if gc:
        payload["generationConfig"] = gc
    return payload


def get_model_name(request: ChatRequest) -> str:
    """Extract the raw model name for the URL path."""
    return request.model


# --- internals ---


def _convert_messages(
    messages: list,
) -> Tuple[str, List[Dict[str, Any]]]:
    """OpenAI messages → (systemInstruction, contents)."""
    system_parts: List[str] = []
    contents: List[Dict[str, Any]] = []
    call_names: Dict[str, str] = {}
    pending_names: List[str] = []  # legacy fallback only

    for m in messages:
        role = m.role
        if role == "system":
            text = m.content or ""
            if text:
                system_parts.append(text)
            continue
        if role == "developer":
            continue
        if role == "user":
            contents.append({"role": "user", "parts": _content_to_parts(m.content)})
        elif role == "assistant":
            parts = _content_to_parts(m.content or "")
            for tc in (m.tool_calls or []):
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                name = fn.get("name", "")
                call_id = tc.get("id") if isinstance(tc, dict) else None
                if call_id:
                    call_names[str(call_id)] = name
                else:
                    pending_names.append(name)
                try:
                    args = json.loads(fn.get("arguments", "{}") or "{}")
                except Exception:
                    args = {}
                parts.append({"functionCall": {"name": name, "args": args}})
            # Only keep non-empty parts (skip empty text added by _content_to_parts
            # when content is None/empty but tool_calls are present)
            filtered = [p for p in parts if not (p.get("text") == "" and len(p) == 1)]
            if filtered:
                contents.append({"role": "model", "parts": filtered})
            else:
                contents.append({"role": "model", "parts": [{"text": ""}]})
        elif role == "tool":
            if m.tool_call_id:
                name = call_names.get(m.tool_call_id, m.name or "unknown")
            else:
                name = m.name or (pending_names.pop(0) if pending_names else "unknown")
            content = m.content or ""
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                except Exception:
                    parsed = {"result": content}
            else:
                parsed = content if isinstance(content, dict) else {"result": content}
            contents.append({
                "role": "user",
                "parts": [{"functionResponse": {"name": name, "response": parsed}}],
            })
    return "".join(system_parts), contents


def _content_to_parts(content: Any) -> List[Dict[str, Any]]:
    """OpenAI content string or array → Gemini parts list."""
    if isinstance(content, str):
        return [{"text": content}] if content else [{"text": ""}]
    if not isinstance(content, list):
        return [{"text": str(content) if content else ""}]
    parts: List[Dict[str, Any]] = []
    for c in content:
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        if t == "text":
            parts.append({"text": c.get("text", "")})
        elif t == "image_url":
            url = c.get("image_url", {}).get("url", "")
            inline = _data_url_to_inline(url)
            if inline:
                parts.append({"inline_data": inline})
        elif t == "input_audio":
            a = c.get("input_audio", {})
            data = str(a.get("data", ""))
            if "," in data:
                data = data.split(",", 1)[-1]
            parts.append({"inline_data": {
                "mime_type": a.get("format", "audio/mpeg"), "data": data
            }})
    return parts or [{"text": ""}]


def _data_url_to_inline(url: str) -> Optional[Dict[str, str]]:
    m = re.match(r"data:([^;,]+);base64,(.+)", url, re.S)
    if m:
        return {"mime_type": m.group(1), "data": m.group(2)}
    return None


def _convert_tools(tools: list) -> List[Dict[str, Any]]:
    result = []
    for t in tools:
        if not isinstance(t, dict) or t.get("type") != "function":
            continue
        fn = t.get("function", {})
        params = fn.get("parameters") or {"type": "object"}
        result.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": params,
        })
    return result


def _convert_tool_choice(tc: Any, fcc: dict) -> dict:
    if isinstance(tc, dict) and tc.get("type") == "function":
        fcc["mode"] = "ANY"
        name = tc.get("function", {}).get("name")
        if name:
            fcc["allowed_function_names"] = [name]
    elif isinstance(tc, str) and tc.lower() == "required":
        fcc["mode"] = "ANY"
    elif isinstance(tc, str) and tc.lower() == "none":
        fcc["mode"] = "NONE"
    return fcc


def _build_generation_config(request: ChatRequest) -> Dict[str, Any]:
    gc: Dict[str, Any] = {}
    if request.temperature is not None:
        gc["temperature"] = request.temperature
    if request.max_tokens is not None:
        gc["maxOutputTokens"] = request.max_tokens
    mc = getattr(request, "max_completion_tokens", None)
    if mc is not None and "maxOutputTokens" not in gc:
        gc["maxOutputTokens"] = mc
    tp = getattr(request, "top_p", None)
    if tp is not None:
        gc["topP"] = tp
    stop = getattr(request, "stop", None)
    if stop is not None:
        if isinstance(stop, str):
            stop = [stop]
        gc["stopSequences"] = [s for s in stop if isinstance(s, str)]

    # thinking
    re_val = str(getattr(request, "reasoning_effort", None) or "").lower()
    thinking = None
    if re_val in _THINK_LEVELS:
        thinking = _THINK_LEVELS[re_val]
    if thinking:
        gc["thinkingConfig"] = {"thinkingLevel": thinking}
    return gc
