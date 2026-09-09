"""Protocol constants and thoughtSignature resolver.

querySignature: fixed frontend constant (NOT an auth signature).
thoughtSignature: pseudo-signature sentinel for history parts with functionCall/thought.
"""
from __future__ import annotations

import base64
from typing import List, Optional

# -- querySignature (fixed, from Go payload.go) --
QUERY_SIGNATURE = "2/l8eCsMMY49imcDQ/lwwXyL8cYtTjxZBF2dNqy69LodY="
OPERATION_NAME = "StreamGenerateContentAnonymous"

# -- thoughtSignature sentinel --
SKIP_THOUGHT_SENTINEL = "skip_thought_signature_validator"
_SKIP_THOUGHT_SENTINEL_B64 = base64.b64encode(SKIP_THOUGHT_SENTINEL.encode()).decode()

# -- Chrome 150 fingerprint --
ANON_BASE_URL = "https://cloudconsole-pa.clients6.google.com"
BATCH_GRAPHQL_PATH = "/v3/entityServices/AiplatformEntityService/schemas/AIPLATFORM_GRAPHQL:batchGraphql"

# -- Model name resolver (normalize path prefixes) --
def trim_gemini_path_prefix(model: str) -> str:
    """Strip GCP path prefixes: models/, publishers/*/models/."""
    m = model.strip()
    lower = m.lower()
    if lower.startswith("models/"):
        return m[len("models/"):]
    if "/models/" in lower:
        idx = lower.index("/models/")
        return m[idx + len("/models/"):]
    return m

# -- thoughtSignature application --

def apply_thought_signature(text: str, is_thought: bool = False) -> Optional[str]:
    """Return the sentinel thoughtSignature for thought parts or function call parts."""
    if is_thought:
        return _SKIP_THOUGHT_SENTINEL_B64
    return None

def ensure_base64_sig(sig: str) -> str:
    """Normalize a thoughtSignature to valid base64."""
    if sig == SKIP_THOUGHT_SENTINEL:
        return _SKIP_THOUGHT_SENTINEL_B64
    try:
        decoded = base64.b64decode(sig)
        if base64.b64encode(decoded).decode() == sig:
            return sig
    except Exception:
        pass
    return base64.b64encode(sig.encode()).decode()

# -- Content role normalization --

def sanitize_contents_role(contents: List[dict]) -> List[dict]:
    """Ensure all content entries have a non-empty role."""
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
        # FunctionResponse turns are never merged with text turns
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
    """Drop entries with no parts after filtering."""
    result = []
    for c in contents:
        parts = [p for p in c.get("parts", []) if _part_has_content(p)]
        if parts:
            result.append({**c, "parts": parts})
    return result

def _part_has_content(p: dict) -> bool:
    """Check if a part has any meaningful content."""
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
