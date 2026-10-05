"""Capability admission checker for Anonymous Vertex nodes (ANON-009-B).

Second real AdmissionChecker: does the endpoint behave like a
Vertex-style generate-content API?  One fixed, minimal, unauthenticated
capability probe — no OAuth, no bearer token, no cookie, no refresh
token, no reCAPTCHA, no browser automation, and NO real user content.

    NodeDefinition -> AnonymousVertexCapabilityChecker
                   -> NodeAdmissionResult (reason token)
                   -> FailureClassifier -> AdmissionPolicy

Failure reason tokens (quarantine is the policy's decision, not ours):

    capability_not_found          endpoint answered 404
    invalid_response              body is not JSON (e.g. HTML)
    invalid_capability_response   JSON but not a Vertex-shaped payload
    http_status_<code>            other 4xx / 5xx answers
    timeout / network_error       transport-level failures
    invalid_endpoint              definition carries no probeable endpoint

``asyncio.CancelledError`` is never captured — it propagates, exactly like
the connectivity checker and the AdmissionRunner contract.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from providers.anonymous_vertex.admission import (
    NodeAdmissionResult,
    NodeAdmissionState,
)
from providers.anonymous_vertex.node_definitions import NodeDefinition

__all__ = ["AnonymousVertexCapabilityChecker"]

DEFAULT_TIMEOUT_SECONDS = 15.0

#: Fixed capability-probe path (v1): a Vertex-style generateContent route.
_PROBE_PATH = "/v1internal:generateContent"

#: Minimal probe payload — a fixed "ping", never real user content.
_PROBE_PAYLOAD: Dict[str, Any] = {
    "contents": [
        {
            "role": "user",
            "parts": [{"text": "ping"}],
        }
    ]
}

_PROBE_HEADERS = {"Content-Type": "application/json"}


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
    """Schema-only verdict: does the payload look like a Vertex response?

    Accepts the standard ``{"candidates": [...]}`` shape.  Pure predicate:
    never mutates the payload, never stores it, never parses model lists.
    """
    if not isinstance(payload, dict):
        return False
    return isinstance(payload.get("candidates"), list)


class AnonymousVertexCapabilityChecker:
    """Probes Vertex-style API behaviour of a node's endpoint."""

    def __init__(
        self,
        http_client: Any,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._http = http_client
        self._timeout_seconds = float(timeout_seconds)

    async def check(self, node: NodeDefinition) -> NodeAdmissionResult:
        url = self._endpoint_url(node)
        if url is None:
            return _failed("invalid_endpoint")

        try:
            response = await self._http.post(
                url + _PROBE_PATH,
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
        if status == 404:
            return _failed("capability_not_found")
        if not 200 <= status < 400:
            return _failed(f"http_status_{status}")

        payload = self._parse_json(response)
        if payload is _NOT_JSON:
            return _failed("invalid_response")
        if not _is_vertex_capability_response(payload):
            return _failed("invalid_capability_response")
        return _ready()

    @staticmethod
    def _parse_json(response: Any) -> Any:
        """Parse the response body as JSON; ``_NOT_JSON`` sentinel on any
        decode failure (HTML error pages, empty bodies, ...)."""
        try:
            return response.json()
        except Exception:  # noqa: BLE001 - any decode error is "not JSON"
            return _NOT_JSON

    @staticmethod
    def _endpoint_url(node: NodeDefinition) -> Optional[str]:
        """Build the probe base URL from the definition's own fields."""
        proxy = node.proxy or {}
        scheme = str(proxy.get("scheme") or "").strip().lower()
        host = proxy.get("host")
        port = proxy.get("port")
        if not scheme or not host or not port:
            return None
        if scheme == "direct":
            return None
        return f"{scheme}://{host}:{port}"


#: Sentinel returned by :meth:`_parse_json` for undecodable bodies.
_NOT_JSON = object()

