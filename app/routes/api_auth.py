"""Client API key authentication for the public /v1 OpenAI-compatible
routes (GATEWAY-002).

Boundary contract:

* ``/v1/models`` and ``/v1/chat/completions`` pass through the single
  ``require_client_auth`` dependency, which runs BEFORE any scheduler
  interaction — an unauthenticated request never reaches
  ``list_models`` / ``chat_completion`` / ``stream_chat``;
* ``/admin`` keeps its own ``ADMIN_TOKEN`` gate — the two boundaries are
  independent and a client API key never grants admin access;
* comparison is constant-time (``hmac.compare_digest``); the submitted
  key is never echoed in error bodies and never logged;
* default config keeps ``api_auth`` DISABLED so local development starts
  unchanged; production opts in explicitly and fail-closed (enabling
  with zero keys is a startup configuration error).

Config::

    api_auth:
      enabled: true
      api_keys: ["sk-..."]      # or GEMINI_GATEWAY_API_KEYS (comma-separated)
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse


class ClientAuthError(Exception):
    """Raised by the /v1 auth seam; rendered as an OpenAI-style 401 by
    the registered exception handler (never echoes the submitted key)."""

    def __init__(self, message: str, code: str) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


def client_auth_error_response(request: Request, exc: ClientAuthError) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "message": exc.message,
                "type": "invalid_request_error",
                "code": exc.code,
            }
        },
        headers={"WWW-Authenticate": "Bearer"},
    )

__all__ = [
    "ApiAuthSettings",
    "build_api_auth_settings",
    "require_client_auth",
]

#: Shared with the credential store's env precedent (AUTH-009).
API_KEYS_ENV = "GEMINI_GATEWAY_API_KEYS"


@dataclass(frozen=True)
class ApiAuthSettings:
    """Immutable /v1 client-auth configuration snapshot."""

    enabled: bool
    api_keys: Tuple[str, ...] = ()


def build_api_auth_settings(config: Mapping[str, Any]) -> ApiAuthSettings:
    """Parse the ``api_auth`` config section (fail-closed on nonsense)."""
    section = config.get("api_auth")
    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        raise ValueError(
            "api_auth config section must be a mapping, got "
            f"{type(section).__name__}"
        )
    enabled = bool(section.get("enabled", False))

    keys: List[str] = []
    configured = section.get("api_keys") or []
    if not isinstance(configured, (list, tuple)):
        raise ValueError("api_auth.api_keys must be a list of strings")
    for key in configured:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("api_auth.api_keys entries must be non-empty strings")
        keys.append(key.strip())
    env_value = os.environ.get(API_KEYS_ENV, "")
    for key in env_value.split(","):
        key = key.strip()
        if key:
            keys.append(key)

    # de-duplicate, preserving order
    unique: Dict[str, None] = {}
    for key in keys:
        unique.setdefault(key, None)
    api_keys = tuple(unique)

    if enabled and not api_keys:
        raise ValueError(
            "api_auth.enabled=true requires at least one API key "
            f"(api_auth.api_keys or {API_KEYS_ENV})"
        )
    return ApiAuthSettings(enabled=enabled, api_keys=api_keys)


async def require_client_auth(request: Request) -> None:
    """Auth seam for /v1/* — raises :class:`ClientAuthError` (rendered as
    an OpenAI-style 401, key-echo-free) BEFORE the route handler touches
    the scheduler.  A disabled/absent settings snapshot keeps the
    local-dev default (no gating)."""
    settings = getattr(request.app.state, "api_auth", None)
    if settings is None or not settings.enabled:
        return

    header = request.headers.get("authorization")
    if not header:
        raise ClientAuthError(
            "missing API key; provide 'Authorization: Bearer <api-key>'",
            "missing_api_key",
        )
    scheme, _, supplied = header.partition(" ")
    supplied = supplied.strip()
    if scheme.lower() != "bearer" or not supplied:
        raise ClientAuthError(
            "invalid authorization scheme; expected 'Bearer <api-key>'",
            "invalid_authorization_scheme",
        )
    if not any(
        hmac.compare_digest(supplied, key) for key in settings.api_keys
    ):
        raise ClientAuthError("invalid API key", "invalid_api_key")
    return  # authenticated
