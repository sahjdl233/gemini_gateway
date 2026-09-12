"""OpenAI ChatRequest -> Code Assist generateContent envelope.

Outer envelope (TASK-007 §8.1):
    {"model": ..., "project": <cloudaicompanionProject>, "request": {...}}

Inner request is a standard Gemini generateContent body:
    contents / systemInstruction / tools / toolConfig / generationConfig /
    safetySettings (10 x BLOCK_NONE).

Thinking follows TASK-007 §8.4 only:
  - gemini-2.5  -> thinkingConfig.thinkingBudget  (number)
  - gemini-3.x  -> thinkingConfig.thinkingLevel   (MINIMAL/LOW/MEDIUM/HIGH)
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from core.models import ChatRequest


_THINK_LEVELS = {
    "minimal": "MINIMAL",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
}

# gemini-2.5 series budgets (TASK-007 §8.4).
_THINK_BUDGETS = {
    "minimal": 128,
    "low": 1024,
    "medium": 8192,
    "high": 16000,
}

# 10 fixed BLOCK_NONE safety settings (TASK-007 §12).
_SAFETY_CATEGORIES = [
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
    "HARM_CATEGORY_CIVIC_INTEGRITY",
    "HARM_CATEGORY_IMPERSONATION",
    "HARM_CATEGORY_IMAGE_SAFETY",
    "HARM_CATEGORY_JAILBREAK",
    "HARM_CATEGORY_VOLUME",
    "HARM_CATEGORY_VULGARITY",
]


def build_payload(request: ChatRequest) -> Dict[str, Any]:
    """Build the inner Gemini generateContent request body."""
    system_text, contents = _convert_messages(request.messages)
    payload: Dict[str, Any] = {"contents": contents}
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": system_text}]}

    if getattr(request, "tools", None):
        gemini_tools = _convert_tools(request.tools)
        if gemini_tools:
            fcc: Dict[str, Any] = {"mode": "AUTO"}
            tc = getattr(request, "tool_choice", None)
            fcc = _convert_tool_choice(tc, fcc)
            payload["tools"] = [{"functionDeclarations": gemini_tools}]
            payload["toolConfig"] = {"functionCallingConfig": fcc}

    gc = _build_generation_config(request)
    if gc:
        payload["generationConfig"] = gc

    payload["safetySettings"] = [
        {"category": cat, "threshold": "BLOCK_NONE"} for cat in _SAFETY_CATEGORIES
    ]
    return payload


def build_envelope(
    request: ChatRequest,
    *,
    project: str,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the outer Code Assist envelope {model, project, request}."""
    return {
        "model": model or request.model,
        "project": project,
        "request": build_payload(request),
    }


def get_model_name(request: ChatRequest) -> str:
    """Raw model name used for the envelope/URL."""
    return request.model


# ---------------------------------------------------------------------------
# messages -> contents / systemInstruction
# ---------------------------------------------------------------------------


def _convert_messages(messages) -> tuple:
    """OpenAI messages -> (systemInstruction text, contents list)."""
    system_parts: List[str] = []
    contents: List[Dict[str, Any]] = []
    pending_names: List[str] = []

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
            contents.append(
                {"role": "user", "parts": _content_to_parts(m.content)}
            )
        elif role == "assistant":
            parts = _content_to_parts(m.content or "")
            for tc in (m.tool_calls or []):
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args else {}
                    except Exception:  # noqa: BLE001
                        args = {}
                if name:
                    pending_names.append(name)
                parts.append(
                    {
                        "functionCall": {
                            "name": name,
                            "args": args if isinstance(args, dict) else {},
                        }
                    }
                )
            filtered = [p for p in parts if not (p.get("text") == "" and len(p) == 1)]
            if filtered:
                contents.append({"role": "model", "parts": filtered})
            else:
                contents.append(
                    {"role": "model", "parts": [{"text": ""}]}
                )
        elif role == "tool":
            name = pending_names.pop(0) if pending_names else "unknown"
            content = m.content or ""
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                except Exception:  # noqa: BLE001
                    parsed = {"result": content}
            elif isinstance(content, dict):
                parsed = content
            else:
                parsed = {"result": content}
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {"functionResponse": {"name": name, "response": parsed}}
                    ],
                }
            )
    return "".join(system_parts), contents


def _content_to_parts(content: Any) -> List[Dict[str, Any]]:
    """OpenAI content string or array -> Gemini parts list."""
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
                parts.append({"inlineData": inline})
        elif t == "input_audio":
            a = c.get("input_audio", {})
            data = str(a.get("data", ""))
            if "," in data:
                data = data.split(",", 1)[-1]
            parts.append(
                {
                    "inlineData": {
                        "mimeType": a.get("format", "audio/mpeg"),
                        "data": data,
                    }
                }
            )
    return parts or [{"text": ""}]


def _data_url_to_inline(url: str) -> Optional[Dict[str, str]]:
    m = re.match(r"data:([^;,]+);base64,(.+)", url, re.S)
    if m:
        return {"mimeType": m.group(1), "data": m.group(2)}
    return None


def _convert_tools(tools: list) -> List[Dict[str, Any]]:
    """OpenAI function tools -> Gemini functionDeclarations."""
    result: List[Dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict) or t.get("type") != "function":
            continue
        fn = t.get("function", {})
        params = fn.get("parameters") or {"type": "object"}
        result.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": params,
            }
        )
    return result


def _convert_tool_choice(tc: Any, fcc: dict) -> dict:
    """OpenAI tool_choice -> Gemini functionCallingConfig."""
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

    thinking = _build_thinking_config(request)
    if thinking:
        gc["thinkingConfig"] = thinking
    return gc


def _build_thinking_config(request: ChatRequest) -> Optional[Dict[str, Any]]:
    """Only the TASK-007-confirmed thinking fields (budget or level)."""
    re_val = str(getattr(request, "reasoning_effort", None) or "").lower()
    if re_val not in _THINK_LEVELS:
        return None
    model = getattr(request, "model", "") or ""
    if _is_gemini_2(model):
        budget = _THINK_BUDGETS.get(re_val)
        if budget is None:
            return None
        cfg: Dict[str, Any] = {"thinkingBudget": budget}
    else:
        cfg = {"thinkingLevel": _THINK_LEVELS[re_val]}
    return cfg


def _is_gemini_2(model: str) -> bool:
    m = re.search(r"gemini-(d)", model)
    if m:
        try:
            return int(m.group(1)) == 2
        except ValueError:
            return False
    return "2.5" in model
