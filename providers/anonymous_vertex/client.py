"""AnonymousVertexClient — protocol client for the batchGraphql endpoint.

Layering:

    AnonymousVertexProvider
            |
            v
    AnonymousVertexClient   (this module)
            |
            v
    AnonymousVertexProtocol (protocol.py: GraphQL envelope)
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

from typing import Any, AsyncIterator, Optional

from providers.anonymous_vertex.errors import (
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
from providers.anonymous_vertex.transport import HTTPTransport


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

    async def post_request(
        self, request: AnonymousVertexRequest, recaptcha_token: str
    ) -> Any:
        """POST the GraphQL envelope for an internal request; return raw response."""
        payload = build_graphql_payload(request, recaptcha_token)
        body = serialize_payload(payload)
        headers = build_xhr_headers()
        return await self._transport.post(
            self.build_url(),
            content=body.encode("utf-8"),
            headers=headers,
        )

    async def stream_content(
        self, request: AnonymousVertexRequest, recaptcha_token: str
    ) -> AsyncIterator[Any]:
        """POST and yield upstream Gemini frames; raise classified errors.

        On a non-200 response the raw body (plus Retry-After, if present) is
        mapped through errors.py so the upper layer sees an
        AnonymousVertexProtocolError subtype (RateLimit/Auth/Unavailable...).
        No retry or resource switching happens here (TASK-002-A section 15).
        """
        resp = await self.post_request(request, recaptcha_token)
        if resp.status_code != 200:
            retry_after = self._extract_retry_after(resp)
            parsed = parse_upstream_error(resp.status_code, resp.content)
            if retry_after is not None and parsed.retry_after is None:
                parsed.retry_after = retry_after
            raise classify_upstream_error(parsed)
        async for frame in iter_chunks(resp.aiter_bytes()):
            yield frame

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

