"""Error mapping from upstream Anonymous Vertex to Gateway core errors.

The scheduler never parses raw upstream errors. This module translates
upstream HTTP status codes, gRPC status strings, and error payloads into
the gateway unified ProviderError hierarchy.
"""
from __future__ import annotations

from typing import Optional

from core.errors import (
    AuthenticationError,
    AuthorizationError,
    InvalidRequestError,
    ModelNotFoundError,
    NetworkError,
    ProviderError,
    RateLimitError,
    UpstreamUnavailableError,
)


class UpstreamVertexError(Exception):
    """Raw error parsed from upstream, used internally for classification."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 500,
        status: str = "",
        kind: str = "",
        retry_after: Optional[float] = None,
        upstream_response: str = "",
    ) -> None:
        self.message = message
        self.status_code = status_code
        self.status = status
        self.kind = kind
        self.retry_after = retry_after
        self.upstream_response = upstream_response
        super().__init__(message)


class AnonymousVertexProtocolError(ProviderError):
    """Base class for Anonymous Vertex protocol-layer errors.

    Every error raised by the protocol/client layer is recognisable by the
    upper layers as an AnonymousVertexProtocolError (in addition to its core
    ProviderError subtype, so the Scheduler keeps working unchanged).
    """

    default_status = 502


class AnonymousVertexAuthError(AuthenticationError, AnonymousVertexProtocolError):
    """Upstream rejected the request for auth reasons (401 / token failed)."""


class AnonymousVertexRateLimitError(RateLimitError, AnonymousVertexProtocolError):
    """Upstream rate-limited the request (429 / RESOURCE_EXHAUSTED)."""


class AnonymousVertexParseError(ProviderError):
    """Upstream stream/frame could not be parsed (malformed JSON)."""

    default_status = 502


class AnonymousVertexConnectionError(NetworkError, AnonymousVertexProtocolError):
    """Connection-level failure talking to the upstream endpoint."""


class AnonymousVertexUnavailableError(UpstreamUnavailableError, AnonymousVertexProtocolError):
    """Upstream returned a 5xx / service unavailable."""


def classify_upstream_error(err: UpstreamVertexError) -> ProviderError:
    """Convert an UpstreamVertexError into a Gateway ProviderError."""
    msg = err.message
    provider = "anonymous_vertex"
    retry_after = err.retry_after

    # kind-based classification (matches Go errors.go)
    if err.kind == "ratelimit" or err.status_code == 429:
        return AnonymousVertexRateLimitError(
            msg,
            provider=provider,
            scope="resource",
            retry_after=retry_after,
        )
    if err.kind == "auth" or err.status_code in (401, 502):
        return AnonymousVertexAuthError(msg, provider=provider)
    if err.kind == "permission" or err.status_code == 403:
        return AuthorizationError(msg, provider=provider)
    if err.kind == "invalid" or err.status_code == 400:
        return InvalidRequestError(msg, provider=provider)
    if err.kind == "notfound" or err.status_code == 404:
        return ModelNotFoundError(msg, provider=provider)
    if err.kind == "network":
        return AnonymousVertexConnectionError(msg, provider=provider)
    if err.kind in ("unavailable",) or err.status_code in (502, 503, 504):
        return AnonymousVertexUnavailableError(msg, provider=provider)
    if err.kind == "internal" or err.status_code >= 500:
        return AnonymousVertexUnavailableError(msg, provider=provider)

    # HTTP status code fallback
    if err.status_code == 429:
        return AnonymousVertexRateLimitError(msg, provider=provider, scope="resource", retry_after=retry_after)
    if err.status_code == 401:
        return AnonymousVertexAuthError(msg, provider=provider)
    if err.status_code == 403:
        return AuthorizationError(msg, provider=provider)
    if err.status_code == 400:
        return InvalidRequestError(msg, provider=provider)
    if err.status_code == 404:
        return ModelNotFoundError(msg, provider=provider)
    if err.status_code >= 500:
        return AnonymousVertexUnavailableError(msg, provider=provider)

    return AnonymousVertexUnavailableError(msg, provider=provider)


def parse_upstream_error(
    status_code: int,
    body: bytes,
) -> UpstreamVertexError:
    """Parse upstream error response into an UpstreamVertexError.

    Supports JSON error bodies (string/array/object) and plain text.
    """
    import json

    text = body.decode("utf-8", errors="replace").strip()

    # Try JSON parse
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Non-JSON body (e.g. Cloudflare gateway page)
        return UpstreamVertexError(
            "Upstream non-JSON response: " + text[:200],
            status_code=status_code if status_code else 502,
        )

    # String
    if isinstance(data, str):
        return UpstreamVertexError(
            "Upstream error: " + data[:200],
            status_code=status_code,
        )

    # Array
    if isinstance(data, list):
        for item in data:
            parsed = _parse_error_obj(item, status_code)
            if parsed is not None:
                return parsed
        return UpstreamVertexError(
            "Upstream returned error array",
            status_code=status_code,
        )

    # Object
    return _parse_error_obj(data, status_code) or UpstreamVertexError(
        "Unknown upstream error format",
        status_code=status_code,
    )


def _parse_error_obj(data: dict, status_code: int) -> Optional[UpstreamVertexError]:
    """Extract an UpstreamVertexError from a single error object dict."""
    import re

    # Nested error object (Google API style)
    err_obj = data.get("error")
    if isinstance(err_obj, dict):
        msg = err_obj.get("message", "Unknown error")
        code = err_obj.get("code", status_code)
        status = err_obj.get("status", "")
        return _classify_by_status(str(msg), int(code) if code else status_code, status)

    # GraphQL errors array
    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            ext = first.get("extensions", {})
            if isinstance(ext, dict):
                ext_status = ext.get("status", {})
                if isinstance(ext_status, dict):
                    code = ext_status.get("code", status_code)
                    status = ext_status.get("status", "")
                    msg = ext_status.get("message", first.get("message", ""))
                    # Check safety
                    fr = ext_status.get("finishReason", "")
                    if _is_safety_finish(fr):
                        return UpstreamVertexError(
                            str(msg), status_code=400, status="SAFETY"
                        )
                    return _classify_by_status(str(msg), int(code) if code else status_code, str(status))
            msg = first.get("message", "Unknown error")
            code = first.get("code", status_code)
            status = first.get("status", "")
            return _classify_by_status(str(msg), int(code) if code else status_code, str(status))

    # Flat format
    fr = data.get("finishReason", "")
    if _is_safety_finish(fr):
        return UpstreamVertexError(
            str(data.get("message", "Blocked by safety")),
            status_code=400,
            status="SAFETY",
        )

    br = data.get("blockReason", "")
    if br and br.upper() != "BLOCKED_REASON_UNSPECIFIED":
        return UpstreamVertexError(
            str(data.get("message", "Blocked by safety")),
            status_code=400,
            status=br.upper(),
        )

    if "code" in data:
        return _classify_by_status(
            str(data.get("message", "Unknown error")),
            int(data["code"]) if data["code"] else status_code,
            str(data.get("status", "")),
        )
    if "message" in data:
        msg = data["message"]
        if _is_safety_finish(msg):
            return UpstreamVertexError(msg, status_code=400, status="SAFETY")
        return _classify_by_status(msg, status_code, "")

    return None


SAFETY_FINISH_REASONS = {
    "SAFETY",
    "RECITATION",
    "PROHIBITED_CONTENT",
    "SPII",
    "BLOCKLIST",
    "IMAGE_SAFETY",
}


def _is_safety_finish(fr: str) -> bool:
    return fr.upper().strip() in SAFETY_FINISH_REASONS


def _classify_by_status(
    msg: str,
    code: int,
    status: str,
) -> UpstreamVertexError:
    """Classify error by HTTP/gRPC status code."""
    st = status.upper().strip()
    if st in ("RESOURCE_EXHAUSTED",) or code == 429:
        return UpstreamVertexError(msg, status_code=429, status=st, kind="ratelimit")
    if st in ("UNAUTHENTICATED",) or code == 401:
        return UpstreamVertexError(msg, status_code=code, status=st, kind="auth")
    if st in ("PERMISSION_DENIED",) or code == 403:
        return UpstreamVertexError(msg, status_code=code, status=st, kind="permission")
    if st in ("INVALID_ARGUMENT",) or code == 400:
        return UpstreamVertexError(msg, status_code=code, status=st, kind="invalid")
    if st in ("NOT_FOUND",) or code == 404:
        return UpstreamVertexError(msg, status_code=code, status=st, kind="notfound")
    if st in ("UNAVAILABLE",) or code == 503:
        return UpstreamVertexError(msg, status_code=code, status=st, kind="unavailable")
    if code >= 500:
        return UpstreamVertexError(msg, status_code=code, status=st, kind="server")
    if code >= 400:
        return UpstreamVertexError(msg, status_code=code, status=st, kind="client")
    return UpstreamVertexError(msg, status_code=code or 500, status=st, kind="server")
