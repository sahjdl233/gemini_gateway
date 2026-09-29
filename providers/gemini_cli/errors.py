"""Gemini CLI (Code Assist) error mapping.

Translates cloudcode-pa.googleapis.com upstream errors - including the
quota-reset signals specific to the Code Assist protocol - into the core
ProviderError hierarchy so the Scheduler never sees raw HTTP/Google text.

429 handling per TASK-007:
  1. details[].metadata.quotaResetTimeStamp  (ISO8601 timestamp)
  2. details[].metadata.quotaResetDelay        ("13h19m1.20964964s")
  3. message text  ("Your quota will reset after 6h 30m 15s.")
  4. fallback: 4 hours (RESOURCE_EXHAUSTED default)
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

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


class GeminiCliProtocolError(ProviderError):
    default_status = 502


class GeminiCliConfigError(ProviderError):
    """Invalid or contradictory gemini_cli configuration.

    Raised while WIRING the provider (before any request is served), so a
    misconfigured deployment fails at startup instead of silently routing
    traffic through the wrong egress.
    """

    default_status = 500


class GeminiCliAuthError(AuthenticationError, GeminiCliProtocolError):
    pass


class GeminiCliRateLimitError(RateLimitError, GeminiCliProtocolError):
    pass


class GeminiCliUnavailableError(UpstreamUnavailableError, GeminiCliProtocolError):
    pass


class GeminiCliNetworkError(NetworkError, GeminiCliProtocolError):
    pass


class GeminiCliTimeoutError(TimeoutError, GeminiCliProtocolError):
    pass


class GeminiCliParseError(GeminiCliProtocolError):
    pass


_DEFAULT_RESET_SECONDS = 4 * 3600

_DURATION_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?")


def _parse_iso_timestamp(value: str) -> Optional[float]:
    text = value.strip()
    try:
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _all_metadata(error: Any) -> list:
    """Yield every metadata dict from the error (flat + details[])."""
    if isinstance(error, dict):
        meta = error.get("metadata")
        if isinstance(meta, dict):
            yield meta
        details = error.get("details")
        if isinstance(details, list):
            for d in details:
                if isinstance(d, dict):
                    inner = d.get("metadata")
                    if isinstance(inner, dict):
                        yield inner


def parse_reset_seconds(
    status_code: int,
    body: Any,
    *,
    now: Optional[float] = None,
) -> Optional[float]:
    if status_code != 429:
        return None
    import time as _time
    now_s = now if now is not None else _time.time()
    error = _error_object(body)
    text = _error_text(body)

    for meta in _all_metadata(error):
        ts = _first_string(meta.get("quotaResetTimeStamp"))
        if ts:
            epoch = _parse_iso_timestamp(ts)
            if epoch is not None:
                return max(0.0, epoch - now_s)
        delay = _first_string(meta.get("quotaResetDelay"))
        if delay:
            seconds = _parse_duration(delay)
            if seconds is not None:
                return max(0.0, seconds)

    m = re.search(r"quota will reset after\s+([0-9hms ]+)", text, re.IGNORECASE)
    if m is None:
        m = re.search(r"quota will reset in\s+([0-9hms ]+)", text, re.IGNORECASE)
    if m:
        seconds = _parse_duration(m.group(1))
        if seconds is not None:
            return max(0.0, seconds)
    return None


def default_reset_seconds() -> float:
    return float(_DEFAULT_RESET_SECONDS)


def extract_retry_after(resp: Any) -> Optional[float]:
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
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


def classify_http_error(status_code: int, body: Any, *, provider: str = "gemini_cli", resource_id: Optional[str] = None) -> ProviderError:
    if status_code == 401:
        return GeminiCliAuthError("gemini_cli 401: invalid OAuth token", provider=provider, resource_id=resource_id, scope="account")
    if status_code == 403:
        return AuthorizationError("gemini_cli 403: not authorized", provider=provider, resource_id=resource_id, scope="resource")
    if status_code == 404:
        return ModelNotFoundError("gemini_cli 404: model not found", provider=provider, resource_id=resource_id)
    if status_code == 400:
        return InvalidRequestError("gemini_cli 400: " + _short_text(_error_text(body)), provider=provider, resource_id=resource_id)
    if status_code == 429:
        retry_after = parse_reset_seconds(status_code, body)
        if retry_after is None:
            retry_after = default_reset_seconds()
        return GeminiCliRateLimitError("gemini_cli 429: quota exhausted", provider=provider, resource_id=resource_id, scope="resource", retry_after=retry_after)
    if status_code in (500, 502, 503):
        return GeminiCliUnavailableError("gemini_cli " + str(status_code) + ": upstream unavailable", provider=provider, resource_id=resource_id)
    if status_code >= 500:
        return GeminiCliUnavailableError("gemini_cli " + str(status_code) + ": upstream error", provider=provider, resource_id=resource_id)
    return GeminiCliProtocolError("gemini_cli " + str(status_code) + ": " + _short_text(_error_text(body)), provider=provider, resource_id=resource_id)


def classify_transport_error(exc: Exception, *, provider: str = "gemini_cli", resource_id: Optional[str] = None) -> ProviderError:
    import httpx
    if isinstance(exc, httpx.TimeoutException):
        return GeminiCliTimeoutError("gemini_cli timeout", provider=provider, resource_id=resource_id)
    return GeminiCliNetworkError("gemini_cli network error: " + str(exc)[:200], provider=provider, resource_id=resource_id)


def _error_object(body: Any) -> Dict[str, Any]:
    data = _as_json(body)
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return error
    return {}


def _as_json(body: Any) -> Any:
    if isinstance(body, dict):
        return body
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return {"message": body}
    return {}


def _error_text(body: Any) -> str:
    data = _as_json(body)
    if not isinstance(data, dict):
        return str(data)[:300]
    error = data.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if message:
            return str(message)
        status = error.get("status")
        return str(status) if status else str(error)[:300]
    message = data.get("message")
    if message:
        return str(message)
    return str(data)[:300]


def _first_string(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list) and value and isinstance(value[0], str):
        return value[0].strip()
    if isinstance(value, dict):
        for key in ("seconds", "timestamp", "value"):
            if isinstance(value.get(key), str):
                return value[key].strip()
    return None


def _parse_duration(text: str) -> Optional[float]:
    t = text.strip().rstrip(".")
    if not t:
        return None
    try:
        return float(t)
    except (TypeError, ValueError):
        pass
    m = _DURATION_RE.fullmatch(t.replace(" ", ""))
    if not m or not any(m.groups()):
        return None
    hours, minutes, seconds = m.groups()
    total = 0.0
    if hours:
        total += float(hours) * 3600
    if minutes:
        total += float(minutes) * 60
    if seconds:
        total += float(seconds)
    return total


def _short_text(text: str) -> str:
    return (text or "unknown upstream error")[:300]
