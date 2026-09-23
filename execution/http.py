"""Shared persistent HTTP execution backend (TASK-ARCH-003 Phase 1).

One ``HttpExecutionBackend`` owns exactly ONE ``httpx.AsyncClient`` for the
whole backend lifetime, so connection pooling, keep-alive, proxy, TLS and
transport configuration belong to the backend, never to a Resource and never
to a single request.

Sharing boundary::

    AntigravityProvider
        -> HttpExecutionBackend
             -> ONE httpx.AsyncClient
                    resource A1
                    resource A2
                    resource A3

The backend deliberately owns no identity material. Access tokens, refresh
tokens, client ids/secrets, cookies and auth_user are request-level inputs
resolved by the Provider at call time (Phase 1: from the Resource; Phase 2:
from the Management Layer).

Transport ownership semantics (deliberate):

* ``transport=None`` and no injected client -> the backend builds and OWNS an
  AsyncClient; ``close()`` shuts it down.
* ``client=client`` -> the backend borrows the transport and does NOT close
  it, unless ``owned=True`` is passed explicitly.
* ``transport=transport`` always wins over the default transport. Silently
  falling back to a live network transport would defeat the injection.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Protocol

import httpx

from transport.proxy import ProxyConfig

# Default upstream budget. A per-request override stays additive on top of
# this value, so every request remains bounded.
DEFAULT_TIMEOUT_SECONDS = 30.0

# 1 vCPU / 1 GB target: bound the pool instead of letting one connection per
# in-flight request accumulate.
DEFAULT_MAX_CONNECTIONS = 20
DEFAULT_MAX_KEEPALIVE_CONNECTIONS = 8

DEFAULT_USER_AGENT = "personal-ai-gateway/0.1"
DEFAULT_HEADERS: Dict[str, str] = {"User-Agent": DEFAULT_USER_AGENT}

# Identity material that must never live on a backend.
_CREDENTIAL_ATTRIBUTES = frozenset({
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "token",
    "client_secret",
    "client_id",
    "secret",
    "cookie",
    "cookies",
    "auth_user",
    "credential",
    "credentials",
    "session_token",
})


class HttpxClient(Protocol):
    """Subset of ``httpx.AsyncClient`` this backend relies on."""

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Optional[Mapping[str, Any]] = None,
        content: Optional[bytes] = None,
        headers: Optional[Mapping[str, str]] = None,
        params: Optional[Mapping[str, Any]] = None,
        timeout: Any = ...,
    ) -> Any:
        ...

    async def aclose(self) -> None:
        ...


def _build_limits(
    max_connections: int,
    max_keepalive_connections: int,
) -> httpx.Limits:
    """Bounded pool for a small deployment (1 vCPU / 1 GB).

    ``limits=`` is the ONLY channel that actually sizes the connection pool.
    ``max_connections`` is not an ``httpx.AsyncClient`` keyword argument;
    passing it raises instead of bounding the pool.
    """
    return httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive_connections,
    )


def _build_async_client(
    *,
    timeout_seconds: float,
    proxy: Optional[ProxyConfig],
    verify_tls: bool,
    follow_redirects: bool,
    max_connections: int,
    max_keepalive_connections: int,
    base_headers: Optional[Mapping[str, str]],
    transport: Any = None,
) -> httpx.AsyncClient:
    """Build one persistent AsyncClient for the backend's lifetime.

    ``transport`` always wins when provided -- it is the test double or a
    future custom transport, and falling back to a live network transport
    would silently defeat the injection.
    """
    headers = {**DEFAULT_HEADERS, **(base_headers or {})}
    limits = _build_limits(max_connections, max_keepalive_connections)

    if transport is not None:
        # Injected transport: the pool is already configured by whoever
        # built it, so limits are not forced here.
        return httpx.AsyncClient(
            timeout=timeout_seconds,
            transport=transport,
            verify=verify_tls,
            follow_redirects=follow_redirects,
            headers=headers,
        )

    # No injected transport. With a proxy, httpx builds the per-proxy
    # transports itself and honours ``limits=``; without one, an explicit
    # local transport carries the limits.
    if proxy is not None:
        return httpx.AsyncClient(
            timeout=timeout_seconds,
            proxy=proxy.as_url(),
            verify=verify_tls,
            follow_redirects=follow_redirects,
            limits=limits,
            headers=headers,
        )

    return httpx.AsyncClient(
        timeout=timeout_seconds,
        transport=httpx.AsyncHTTPTransport(limits=limits),
        verify=verify_tls,
        follow_redirects=follow_redirects,
        headers=headers,
    )


class HttpExecutionBackend:
    """Provider-owned HTTP backend wrapping one persistent AsyncClient.

    Pool, proxy, TLS and timeout configuration belong to this backend's
    lifetime, not to a Resource and not to a single request.
    """

    def __init__(
        self,
        *,
        transport: Any = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        proxy: Optional[ProxyConfig] = None,
        verify_tls: bool = True,
        follow_redirects: bool = True,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_keepalive_connections: int = DEFAULT_MAX_KEEPALIVE_CONNECTIONS,
        base_headers: Optional[Mapping[str, str]] = None,
        client: Optional[HttpxClient] = None,
        owned: bool = False,
    ) -> None:
        # Close-once flag: keeps close() idempotent for the whole backend
        # lifetime.
        self._closed = False

        if client is not None:
            # Injected transport: tests, or a future custom transport. The
            # backend only manages channels it builds itself.
            self._client: HttpxClient = client
            self._owns_client = owned
        else:
            self._client = _build_async_client(
                timeout_seconds=timeout_seconds,
                proxy=proxy,
                verify_tls=verify_tls,
                follow_redirects=follow_redirects,
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive_connections,
                base_headers=base_headers,
                transport=transport,
            )
            self._owns_client = True

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
        """Dispatch one request through the shared client.

        Per-request auth belongs in ``headers``. A ``timeout`` override is
        merged onto the backend's base timeout so the request as a whole
        stays bounded; without an override the backend default applies.
        """
        if timeout is None:
            # Ellipsis means "use the client default": no new budget is
            # invented and the backend-wide timeout stays authoritative.
            effective_timeout: Any = Ellipsis
        else:
            base_timeout = getattr(self._client, "timeout", None)
            # httpx applies a request Timeout to every phase EXCEPT those
            # named explicitly, so the pool phase must be copied by hand.
            # Without this, a request-level read timeout would also shrink
            # the pool timeout and starve concurrent requests.
            if base_timeout is None:
                effective_timeout = httpx.Timeout(timeout)
            else:
                effective_timeout = httpx.Timeout(
                    timeout,
                    connect=base_timeout.connect,
                    pool=base_timeout.pool,
                )

        return await self._client.request(
            method,
            url,
            json=dict(json) if json is not None else None,
            content=data,
            headers=dict(headers) if headers else None,
            params=dict(params) if params is not None else None,
            timeout=effective_timeout,
        )

    async def close(self) -> None:
        """Shut down the shared client. Idempotent for repeated calls."""
        if not self._owns_client or self._client is None:
            return
        if self._closed:
            return
        await self._client.aclose()
        self._closed = True

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _CREDENTIAL_ATTRIBUTES:
            raise AttributeError(
                f"{type(self).__name__!r} owns no identity material; "
                f"refusing to set {name!r}. Pass credentials per request."
            )
        super().__setattr__(name, value)

    def __getattr__(self, name: str) -> Any:
        # Called only when normal lookup fails. Keeps attribute access loud
        # instead of returning a silently-wrong default.
        if name.startswith("_"):
            raise AttributeError(name)
        raise AttributeError(f"{type(self).__name__!r} has no attribute {name!r}")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"closed={self._closed}, "
            f"owns_client={self._owns_client})"
        )
