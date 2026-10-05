"""HTTP transport boundary for the Anonymous Vertex protocol layer.

The Client depends on a minimal async HTTP transport interface, never on
httpx directly.  This keeps provider/protocol code free of transport and
egress concerns.

Streaming lifecycle (ANON-002)::

    HTTPTransport.stream()      -> async context manager yielding a response
        response.status_code    (available before the body is read)
        response.aiter_bytes()  (true incremental body consumption)
        response.aread()        (only for small non-200 error bodies)
    context exit                -> response/connection closed

The response body is never buffered by the transport on the 200 path: the
client consumes ``aiter_bytes()`` directly so upstream bytes reach the
NDJSON parser as they arrive.

Layering::

    AnonymousVertexClient
            |
            v
    HTTP Transport   (this module)
            |
            v
    Proxy Pool / sing-box   (future task)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncContextManager, AsyncIterator, Dict, Optional, Protocol


class StreamingHTTPResponse(Protocol):
    """Minimal response surface the Client consumes on a streaming call.

    ``status_code`` and ``headers`` must be available without reading the
    body; ``aiter_bytes()`` yields the body incrementally; ``aread()``
    reads the (small) body into memory — used only for non-200 error
    payloads.  Closing is owned by the transport's ``stream()`` context
    manager.
    """

    status_code: int
    headers: Any

    def aiter_bytes(self) -> AsyncIterator[bytes]:
        """Yield the response body incrementally."""
        ...

    async def aread(self) -> bytes:
        """Read the full body (error responses only)."""
        ...


class HTTPTransport(Protocol):
    """Async HTTP contract used by AnonymousVertexClient."""

    async def post(
        self,
        url: str,
        *,
        content: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        """POST url with an optional raw body/headers; return the response."""
        ...

    def stream(
        self,
        url: str,
        *,
        content: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> AsyncContextManager[Any]:
        """Open a POST whose body can be consumed incrementally.

        Returns an async context manager yielding a
        :class:`StreamingHTTPResponse`-compatible object.  The response /
        connection is closed on context exit — normal EOF, parser errors,
        mid-stream disconnects and cancellation included.
        """
        ...


class HttpxTransport:
    """Adapter over an httpx.AsyncClient-compatible object (or a test mock).

    If the wrapped client exposes ``stream()`` (real httpx does), it is
    used so the body is consumed chunk by chunk.  Clients that only
    implement a buffered ``post()`` (existing test mocks) are wrapped into
    an already-complete response — behaviour is identical, only latency
    differs.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def post(
        self,
        url: str,
        *,
        content: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        return await self._client.post(url, content=content, headers=headers)

    @asynccontextmanager
    async def stream(
        self,
        url: str,
        *,
        content: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> AsyncIterator[Any]:
        stream_factory = getattr(self._client, "stream", None)
        if stream_factory is not None:
            async with stream_factory(
                "POST", url, content=content, headers=headers
            ) as response:
                yield response
        else:
            yield await self._client.post(url, content=content, headers=headers)


@asynccontextmanager
async def stream_context(
    transport: Any,
    url: str,
    *,
    content: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
) -> AsyncIterator[Any]:
    """Normalize the two transport shapes into one response context.

    * transports implementing the streaming ``stream()`` contract
      (:class:`HttpxTransport`, streaming test fakes) — the response is
      closed by the transport's own context manager;
    * legacy post-only transports (existing test fakes) — the response is
      already fully buffered; it is yielded as-is.
    """
    stream_factory = getattr(transport, "stream", None)
    if stream_factory is not None:
        async with stream_factory(url, content=content, headers=headers) as response:
            yield response
    else:
        response = await transport.post(url, content=content, headers=headers)
        yield response
