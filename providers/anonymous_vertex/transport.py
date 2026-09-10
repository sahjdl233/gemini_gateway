"""HTTP transport boundary for the Anonymous Vertex protocol layer.

The Client depends on a minimal async HTTP transport interface, never on
httpx directly.  This keeps provider/protocol code free of transport and
egress concerns.

Future layering (NOT implemented in this task)::

    AnonymousVertexClient
            |
            v
    HTTP Transport   (this module)
            |
            v
    Proxy Pool / sing-box   (future task)
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol


class HTTPTransport(Protocol):
    """Minimal async HTTP POST contract used by AnonymousVertexClient.

    The returned response-like object must expose:
      - status_code (int)
      - content (bytes)
      - an optional headers mapping (.get(...))
      - an aiter_bytes() async iterator for streaming bodies
    """

    async def post(
        self,
        url: str,
        *,
        content: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        """POST url with an optional raw body/headers; return the response."""
        ...


class HttpxTransport:
    """Adapter over an httpx.AsyncClient-compatible object (or a test mock)."""

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
