"""Auth / CAPTCHA failure-mapping checker for Anonymous Vertex (ANON-009-C).

Third real AdmissionChecker.  Where the capability checker asks "is this a
Vertex-style API?", this one asks "does the Vertex API allow ANONYMOUS use
through this node?" — via the same fixed, minimal, unauthenticated probe.
No token fetching, no OAuth refresh, no cookie import, no browser
automation, no reCAPTCHA solving: only detection of signals already
present in responses.

    NodeDefinition -> AnonymousVertexAuthChecker
                   -> NodeAdmissionResult (reason token)
                   -> FailureClassifier -> AdmissionPolicy

Failure reason tokens (quarantine is the policy's decision, not ours):

    auth_failed            401, or an UNAUTHENTICATED / PERMISSION_DENIED
                           error status in the response body
    captcha_required       explicit captcha / risk-control signals in the
                           response body (captcha / recaptcha / challenge /
                           risk) — detection only, never a solve attempt
    http_status_<code>     any other non-2xx answer (server errors must
                           not be misread as auth failures)
    invalid_response       2xx body that cannot be interpreted
    timeout / network_error / invalid_endpoint   transport-level failures

``asyncio.CancelledError`` is never captured — it propagates.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from providers.anonymous_vertex.admission import (
    NodeAdmissionResult,
    NodeAdmissionState,
)
from providers.anonymous_vertex.node_definitions import NodeDefinition
from providers.anonymous_vertex.signature import (
    ANONYMOUS_VERTEX_GRAPHQL_ENDPOINT,
)

__all__ = ["AnonymousVertexAuthChecker", "classify_auth_response"]

DEFAULT_TIMEOUT_SECONDS = 15.0

#: Fixed probe: the REAL Anonymous Vertex batchGraphql endpoint, reached
#: THROUGH the node's egress (same target as the capability checker).
_PROBE_URL = ANONYMOUS_VERTEX_GRAPHQL_ENDPOINT
_PROBE_PAYLOAD: Dict[str, Any] = {
    "contents": [
        {
            "role": "user",
            "parts": [{"text": "ping"}],
        }
    ]
}
_PROBE_HEADERS = {"Content-Type": "application/json"}

#: Google error.status values that mean "this node cannot be used
#: anonymously".
_AUTH_ERROR_STATUSES = frozenset({"UNAUTHENTICATED", "PERMISSION_DENIED"})

#: Explicit risk-control signals searched in the response body.
_CAPTCHA_SIGNALS = ("captcha", "recaptcha", "challenge", "risk")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _failed(reason: str) -> NodeAdmissionResult:
    return NodeAdmissionResult(
        state=NodeAdmissionState.FAILED, reason=reason, checked_at=_utcnow()
    )


def _ready() -> NodeAdmissionResult:
    return NodeAdmissionResult(
        state=NodeAdmissionState.READY, reason=None, checked_at=_utcnow()
    )


def _is_vertex_capability_response(payload: Any) -> bool:
    """Schema-only verdict (same contract as the capability checker)."""
    if not isinstance(payload, dict):
        return False
    return isinstance(payload.get("candidates"), list)


def classify_auth_response(status_code: int, payload: object) -> Optional[str]:
    """Pure failure-mapping verdict for one probe response.

    Returns a reason token when the response carries an explicit auth or
    captcha/risk signal, else ``None`` (no signal — the caller falls back
    to generic status handling).  No network, no payload mutation, no
    state writes.
    """
    if status_code == 401:
        return "auth_failed"

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            status = str(error.get("status") or "").upper()
            if status in _AUTH_ERROR_STATUSES:
                return "auth_failed"

    # CAPTCHA / risk-control signals anywhere in the response body —
    # detection only; solving is out of scope by design.
    try:
        body_text = json.dumps(payload, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        body_text = str(payload).lower()
    if any(signal in body_text for signal in _CAPTCHA_SIGNALS):
        return "captcha_required"

    return None


class AnonymousVertexAuthChecker:
    """Probes whether a node's Vertex endpoint allows anonymous use."""

    def __init__(
        self,
        http_client: Any = None,
        *,
        client_factory: Optional[Any] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """``http_client`` is the egress client to probe through; OR pass
        ``client_factory(node) -> client`` for per-node real transports.
        Same injection boundary as before — no client creation in check().
        """
        if http_client is None and client_factory is None:
            raise ValueError(
                "auth checker requires http_client or client_factory"
            )
        self._http = http_client
        self._client_factory = client_factory
        self._timeout_seconds = float(timeout_seconds)

    def _client_for(self, node: NodeDefinition) -> Any:
        if self._client_factory is not None:
            return self._client_factory(node)
        return self._http

    async def check(self, node: NodeDefinition) -> NodeAdmissionResult:
        try:
            # factory failures (e.g. no egress available) are classified
            # like any other probe failure — never allowed to escape
            client = self._client_for(node)
            response = await client.post(
                _PROBE_URL,
                json=_PROBE_PAYLOAD,
                headers=dict(_PROBE_HEADERS),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            return _failed("timeout")
        except httpx.TimeoutException:
            return _failed("timeout")
        except (httpx.HTTPError, OSError):
            return _failed("network_error")

        status = int(getattr(response, "status_code", 0))

        if 200 <= status < 400:
            payload = self._parse_json(response)
            if payload is _NOT_JSON:
                return _failed("invalid_response")
            token = classify_auth_response(status, payload)
            if token is not None:
                return _failed(token)
            if _is_vertex_capability_response(payload):
                return _ready()
            return _failed("invalid_response")

        # Non-2xx: an explicit body signal wins; otherwise keep the plain
        # status token so server errors are never misread as auth errors.
        payload = self._parse_json(response)
        token = (
            None
            if payload is _NOT_JSON
            else classify_auth_response(status, payload)
        )
        if token is not None:
            return _failed(token)
        return _failed(f"http_status_{status}")

    @staticmethod
    def _parse_json(response: Any) -> Any:
        try:
            return response.json()
        except Exception:  # noqa: BLE001 - any decode error is "not JSON"
            return _NOT_JSON



#: Sentinel for undecodable response bodies.
_NOT_JSON = object()
