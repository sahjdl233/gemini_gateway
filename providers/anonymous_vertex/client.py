"""AnonymousVertexClient — protocol client for the batchGraphql endpoint.

Layering:

    AnonymousVertexProvider
            |
            v
    AnonymousVertexClient   (this module)
            |
            v
    AnonymousVertexProtocol (protocol.py)
            |
            v
    HTTP Transport          (transport.py / injected client)

The client owns everything the wire needs: endpoint URL construction,
request headers, envelope serialization, the POST call and upstream error
classification.  The Provider never builds Google payloads or headers; it
hands an AnonymousVertexRequest to the client and converts the returned
Gemini frames into Gateway chunks.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Optional, Tuple

from core.errors import TimeoutError as UpstreamTimeoutError

from providers.anonymous_vertex.errors import (
    AnonymousVertexConnectionError,
    classify_upstream_error,
    parse_upstream_error,
)
from providers.anonymous_vertex.headers import build_xhr_headers
from providers.anonymous_vertex.models import AnonymousVertexRequest
from providers.anonymous_vertex.protocol import (
    build_graphql_payload,
    serialize_payload,
)
from providers.anonymous_vertex.signature import ANONYMOUS_VERTEX_GRAPHQL_ENDPOINT
from providers.anonymous_vertex.streaming import iter_chunks
from providers.anonymous_vertex.transport import HTTPTransport, stream_context


class AnonymousVertexClient:
    """Posts GraphQL envelopes and iterates upstream stream frames."""

    def __init__(
        self,
        transport: HTTPTransport,
        api_key: str = "",
        *,
        endpoint: str = ANONYMOUS_VERTEX_GRAPHQL_ENDPOINT,
    ) -> None:
        self._transport = transport
        self._api_key = api_key
        self._endpoint = endpoint

    def build_url(self) -> str:
        return f"{self._endpoint}?key={self._api_key}&prettyPrint=false"

    def build_request_body(
        self, request: AnonymousVertexRequest, recaptcha_token: str
    ) -> Tuple[str, bytes, Dict[str, str]]:
        """Build (url, encoded body, headers) for one upstream POST."""
        payload = build_graphql_payload(request, recaptcha_token)
        body = serialize_payload(payload)
        headers = build_xhr_headers()
        return self.build_url(), body.encode("utf-8"), headers

    async def post_request(
        self, request: AnonymousVertexRequest, recaptcha_token: str
    ) -> Any:
        """POST the GraphQL envelope for an internal request; return raw response."""
        url, body, headers = self.build_request_body(request, recaptcha_token)
        return await self._transport.post(url, content=body, headers=headers)

    async def stream_content(
        self, request: AnonymousVertexRequest, recaptcha_token: str
    ) -> AsyncIterator[Any]:
        """POST and yield upstream Gemini frames; raise classified errors.

        The request is opened through the transport's streaming context so
        the NDJSON body is consumed incrementally (ANON-002): frames are
        yielded as their bytes arrive, never after a full-body read.  On a
        non-200 response the (small) error body is read and mapped through
        errors.py so the upper layer sees an AnonymousVertexProtocolError
        subtype (RateLimit/Auth/Unavailable...).  Connection-level failures
        (timeout, reset, mid-stream disconnect) are mapped to
        NetworkError/TimeoutError subtypes so the Scheduler can cool the
        resource down and fall back.  The response is closed by the
        transport context on every exit path (EOF, parser error,
        disconnect, cancellation).  No retry or resource switching happens
        here (TASK-002-A section 15).
        """
        import httpx

        url, body, headers = self.build_request_body(request, recaptcha_token)
        try:
            async with stream_context(
                self._transport, url, content=body, headers=headers
            ) as resp:
                if resp.status_code != 200:
                    retry_after = self._extract_retry_after(resp)
                    parsed = parse_upstream_error(
                        resp.status_code, await self._read_body(resp)
                    )
                    if retry_after is not None and parsed.retry_after is None:
                        parsed.retry_after = retry_after
                    raise classify_upstream_error(parsed)
                async for frame in iter_chunks(resp.aiter_bytes()):
                    yield frame
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(
                f"upstream timeout: {exc}", provider="anonymous_vertex"
            ) from exc
        except httpx.HTTPError as exc:
            raise AnonymousVertexConnectionError(
                f"upstream connection failed: {exc}", provider="anonymous_vertex"
            ) from exc

    @staticmethod
    async def _read_body(resp: Any) -> bytes:
        """Read a (non-200) response body without assuming full buffering.

        Streaming responses expose ``aread()``; legacy buffered mock
        responses expose ``.content``.
        """
        aread = getattr(resp, "aread", None)
        if aread is not None:
            return await aread()
        return resp.content

    @staticmethod
    def _extract_retry_after(resp: Any) -> Optional[float]:
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
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
