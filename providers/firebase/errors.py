"""Error mapping: Firebase AI Logic upstream → Gateway ProviderError.

Every upstream error is classified into the core error hierarchy.
Retry-After is extracted when present. The Scheduler never sees raw
HTTP status codes or Google error JSON.
"""
from __future__ import annotations

import json
from typing import Optional

from core.errors import (
    AuthenticationError,
    AuthorizationError,
    InvalidRequestError,
    ModelNotFoundError,
    NetworkError,
    ProviderError,
    RateLimitError,
    TimeoutError,
    UpstreamUnavailableError,
)


class FirebaseProtocolError(ProviderError):
    """Base for Firebase-specific protocol errors."""
    default_status = 502


class FirebaseAuthError(AuthenticationError, FirebaseProtocolError):
    pass


class FirebaseRateLimitError(RateLimitError, FirebaseProtocolError):
    pass


class FirebaseUnavailableError(UpstreamUnavailableError, FirebaseProtocolError):
    pass


class FirebaseNetworkError(NetworkError, FirebaseProtocolError):
    pass


class FirebaseTimeoutError(TimeoutError, FirebaseProtocolError):
    pass


def extract_retry_after(resp: object) -> Optional[float]:
    """Extract Retry-After header as float seconds."""
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    raw = None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except (AttributeError, TypeError):
        return None
    if raw is None:
        return None
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def classify_http_error(
    status_code: int, body: bytes, retry_after: Optional[float] = None
) -> ProviderError:
    """Map an HTTP error response to a ProviderError."""
    msg = _parse_error_message(body)
    provider = "firebase"
    if status_code == 429:
        return FirebaseRateLimitError(
            msg, provider=provider, scope="resource", retry_after=retry_after
        )
    if status_code == 401:
        return FirebaseAuthError(msg, provider=provider)
    if status_code == 403:
        return AuthorizationError(msg, provider=provider)
    if status_code == 404:
        return ModelNotFoundError(msg, provider=provider)
    if status_code == 400:
        return InvalidRequestError(msg, provider=provider)
    if status_code >= 500:
        return FirebaseUnavailableError(msg, provider=provider)
    return FirebaseUnavailableError(msg, provider=provider)


def _parse_error_message(body: bytes) -> str:
    """Best-effort extraction of error message from upstream JSON."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        text = body.decode("utf-8", errors="replace").strip()[:300]
        return text or "unknown upstream error"
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message", "unknown error"))[:300]
    return str(data)[:300]
