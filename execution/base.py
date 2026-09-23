"""Execution backend contract (TASK-ARCH-003 Phase 1).

An ExecutionBackend owns an execution *channel* and its lifecycle:
transport construction, connection pooling, keep-alive, timeouts and
shutdown.

The Scheduler never sees a backend. Only a Provider does::

    Scheduler -> Provider.complete(request, resource)
                        |
                        v
                Backend.execute(...)
                        |
                        v
              persistent transport

Backends must NOT hold identity material (OAuth tokens, API keys,
cookies, auth_user). Per-request auth travels as request-level
arguments, resolved by the Provider from the selected Resource
(temporary, Phase 1) or from the Management Layer (Phase 2).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol


class ExecutionBackend(Protocol):
    """Minimal async execution backend contract."""

    async def execute(
        self,
        method: str,
        url: str,
        *,
        json: Optional[Mapping[str, Any]] = None,
        data: Optional[bytes] = None,
        headers: Optional[Mapping[str, str]] = None,
        params: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Issue one request; return the upstream response object.

        The returned response must expose at least ``status_code``,
        ``text`` and ``json()``, matching httpx semantics.
        """
        ...

    async def close(self) -> None:
        """Release the shared channel. Must be safe to call twice."""
        ...
