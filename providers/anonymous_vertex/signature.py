"""Protocol constants, single source of truth.

querySignature: fixed frontend constant (NOT an auth signature).
thoughtSignature: pseudo-signature sentinel for history parts with functionCall/thought.

Every protocol literal (endpoint, querySignature, operationName, sentinel) is
defined here exactly once; protocol/request/client/headers import from this
module instead of re-hardcoding Google strings.
"""

from __future__ import annotations

import base64
from typing import Optional

# -- GraphQL endpoint (single source; see docs/anonymous-vertex-protocol.md) --
ANON_BASE_URL = "https://cloudconsole-pa.clients6.google.com"
BATCH_GRAPHQL_PATH = "/v3/entityServices/AiplatformEntityService/schemas/AIPLATFORM_GRAPHQL:batchGraphql"
ANONYMOUS_VERTEX_GRAPHQL_ENDPOINT = ANON_BASE_URL + BATCH_GRAPHQL_PATH

# -- querySignature (fixed, from Go payload.go; NOT a dynamic signature) --
QUERY_SIGNATURE = "2/l8eCsMMY49imcDQ/lwwXyL8cYtTjxZBF2dNqy69LodY="
OPERATION_NAME = "StreamGenerateContentAnonymous"

# -- thoughtSignature sentinel --
SKIP_THOUGHT_SENTINEL = "skip_thought_signature_validator"
_SKIP_THOUGHT_SENTINEL_B64 = base64.b64encode(SKIP_THOUGHT_SENTINEL.encode()).decode()

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

