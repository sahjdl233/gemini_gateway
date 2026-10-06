"""Connectivity admission checker for Anonymous Vertex nodes (ANON-009-A).

First real AdmissionChecker: does the node's endpoint accept a basic
connection?  One plain unauthenticated request — no token, no Vertex API,
no reCAPTCHA, no proxy probing beyond the endpoint itself.

    NodeDefinition -> AnonymousVertexConnectivityChecker
                   -> NodeAdmissionResult (reason token)
                   -> FailureClassifier -> AdmissionPolicy

Design rules:

* the HTTP client is INJECTED (``http_client``) — the checker never builds
  one, so tests mock it, and a future transport replacement (curl_cffi /
  node pool clients) plugs in without touching this file;
* the endpoint comes from the NodeDefinition's own proxy fields — no
  string re-parsing, no node_id generation, no definition mutation;
* v1 sends HEAD first; a HEAD-unsupported response (405 / 501) falls back
  to GET.  Success is HTTP 200-399;
* every failure becomes ``FAILED`` with a stable reason token
  (``timeout`` / ``network_error`` / ``http_status_<code>`` /
  ``invalid_endpoint``); quarantine is the policy's decision, not ours;
* ``asyncio.CancelledError`` is never captured — it propagates (the
  AdmissionRunner contract).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from providers.anonymous_vertex.admission import (
    AdmissionChecker,
    NodeAdmissionResult,
    NodeAdmissionState,
)
from providers.anonymous_vertex.node_definitions import NodeDefinition

__all__ = ["AnonymousVertexConnectivityChecker"]

DEFAULT_TIMEOUT_SECONDS = 10.0

#: Fixed connectivity probe target: Google's canonical connectivity-check
#: endpoint (HTTP 204, no auth, designed for exactly this purpose).  The
#: probe travels THROUGH the node's real egress (the injected client), so
#: this URL is never derived from the node's proxy configuration.
CONNECTIVITY_PROBE_URL = "https://connectivitycheck.gstatic.com/generate_204"

#: HEAD responses that mean "method not supported here" -> retry with GET.
_HEAD_FALLBACK_STATUSES = frozenset({405, 501})


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


class AnonymousVertexConnectivityChecker:
    """Probes basic connectivity of a node's endpoint."""

    def __init__(
        self,
        http_client: Any = None,
        *,
        client_factory: Optional[Any] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """``http_client`` is the egress client to probe through; OR pass
        ``client_factory(node) -> client`` so each node probes through its
        OWN real transport (proxy-aware).  Exactly the same injection
        boundary as before — the checker never creates clients itself.
        """
        if http_client is None and client_factory is None:
            raise ValueError(
                "connectivity checker requires http_client or client_factory"
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
            response = await client.head(
                CONNECTIVITY_PROBE_URL, timeout=self._timeout_seconds
            )
            status = int(getattr(response, "status_code", 0))
            if status in _HEAD_FALLBACK_STATUSES:
                # HEAD not supported by this target: fall back to GET
                response = await client.get(
                    CONNECTIVITY_PROBE_URL, timeout=self._timeout_seconds
                )
                status = int(getattr(response, "status_code", 0))
        except asyncio.TimeoutError:
            return _failed("timeout")
        except httpx.TimeoutException:
            return _failed("timeout")
        except (httpx.HTTPError, OSError):
            return _failed("network_error")
        except Exception as exc:  # noqa: BLE001 - never escape the checker
            return _failed(f"checker_error: {exc}")

        if 200 <= status < 400:
            return _ready()
        return _failed(f"http_status_{status}")

