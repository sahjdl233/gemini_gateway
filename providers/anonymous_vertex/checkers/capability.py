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
from providers.anonymous_vertex.signature import (
    ANONYMOUS_VERTEX_GRAPHQL_ENDPOINT,
)

__all__ = ["AnonymousVertexCapabilityChecker"]

DEFAULT_TIMEOUT_SECONDS = 15.0

#: Fixed capability probe: the REAL Anonymous Vertex batchGraphql endpoint,
#: reached THROUGH the node's egress (the injected client).  Never derived
#: from the node's proxy configuration.
_PROBE_URL = ANONYMOUS_VERTEX_GRAPHQL_ENDPOINT

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
                "capability checker requires http_client or client_factory"
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



#: Sentinel returned by :meth:`_parse_json` for undecodable bodies.
_NOT_JSON = object()

