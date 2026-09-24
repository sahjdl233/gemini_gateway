from __future__ import annotations

from typing import Any, Mapping, Optional

import logging

from execution.base import ExecutionBackend
from core.errors import (
    AuthenticationError,
    AuthorizationError,
    InvalidRequestError,
    ModelNotFoundError,
    NetworkError,
    ProtocolError,
    RateLimitError,
    UpstreamUnavailableError,
)

from .resource import AntigravityResource

logger = logging.getLogger(__name__)

BASE_URL = "https://daily-cloudcode-pa.googleapis.com"


class AntigravityClient:
    """Antigravity protocol layer: endpoint shape, headers and payloads.

    TASK-ARCH-003: this class owns no transport. The provider-owned
    ExecutionBackend supplies ONE persistent AsyncClient shared by every
    AntigravityResource, so transport is an O(provider) resource instead of
    an O(resource) one. Auth is resolved per request from the selected
    Resource and travels as a request header only.

    There is deliberately no fallback path that constructs a client here:
    a missing backend is a wiring error and must fail loudly.
    """

    def __init__(
        self,
        *,
        backend: Optional[ExecutionBackend] = None,
        timeout: float = 30.0,
    ) -> None:
        self._backend = backend
        self.timeout = timeout

    async def _request(
        self,
        operation: str,
        resource: AntigravityResource,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        if self._backend is None:
            raise RuntimeError(
                "AntigravityClient needs a provider-owned ExecutionBackend; "
                "the per-request httpx.Client path has been removed."
            )
        endpoint = f"{BASE_URL}/v1internal:{operation}"
        headers = {"Content-Type": "application/json"}
        if resource.access_token:
            headers["Authorization"] = f"Bearer {resource.access_token}"
        resp = await self._backend.execute(
            "POST",
            endpoint,
            json=payload or {},
            headers=headers,
            timeout=self.timeout,
        )
        status_code = resp.status_code
        if status_code >= 400:
            self._raise_http_error(
                status_code,
                str(getattr(resp, "text", ""))[:512],
                resource,
                getattr(resp, "headers", None),
            )
        try:
            data = resp.json()
        except (TypeError, ValueError) as exc:
            raise ProtocolError(
                "Antigravity upstream returned malformed JSON",
                provider="antigravity",
                resource_id=resource.id,
            ) from exc
        if not isinstance(data, dict):
            raise ProtocolError(
                "Antigravity upstream returned a non-object JSON response",
                provider="antigravity",
                resource_id=resource.id,
            )
        return data

    @staticmethod
    async def _stream_error_text(resp: Any, limit: int = 512) -> str:
        """Read a bounded error body and always release the response."""
        parts: list[str] = []
        size = 0
        try:
            async for chunk in resp.aiter_text():
                text = str(chunk)
                if size >= limit:
                    break
                parts.append(text[: limit - size])
                size += len(parts[-1])
        finally:
            await resp.aclose()
        return "".join(parts)

    @staticmethod
    def _raise_http_error(
        status_code: int,
        body: str,
        resource: AntigravityResource,
        headers: Any = None,
    ) -> None:
        error_types = {
            400: InvalidRequestError,
            401: AuthenticationError,
            403: AuthorizationError,
            404: ModelNotFoundError,
            429: RateLimitError,
            500: UpstreamUnavailableError,
            502: NetworkError,
            503: UpstreamUnavailableError,
        }
        error_type = error_types.get(status_code, UpstreamUnavailableError)
        retry_after = None
        if status_code == 429 and headers is not None:
            raw_retry_after = headers.get("retry-after")
            if raw_retry_after is not None:
                try:
                    retry_after = float(raw_retry_after)
                except (TypeError, ValueError):
                    retry_after = None
        raise error_type(
            "Antigravity upstream error HTTP %s: %s"
            % (status_code, body or "<empty response body>"),
            provider="antigravity",
            resource_id=resource.id,
            retry_after=retry_after,
        )

    async def fetch_available_models(
        self,
        resource: AntigravityResource,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        return await self._request(
            "fetchAvailableModels", resource, payload
        )

    async def generate_content(
        self,
        resource: AntigravityResource,
        payload: Mapping[str, Any],
    ) -> Any:
        return await self._request("generateContent", resource, payload)

    async def stream_generate_content(
        self,
        resource: AntigravityResource,
        payload: Mapping[str, Any],
    ) -> Any:
        if self._backend is None:
            raise RuntimeError(
                "AntigravityClient needs a provider-owned ExecutionBackend"
            )
        endpoint = f"{BASE_URL}/v1internal:streamGenerateContent?alt=sse"
        headers = {"Content-Type": "application/json"}
        if resource.access_token:
            headers["Authorization"] = f"Bearer {resource.access_token}"
        resp = await self._backend.execute_stream(
            "POST",
            endpoint,
            json=payload or {},
            headers=headers,
            timeout=self.timeout,
        )
        status_code = resp.status_code
        if status_code >= 400:
            body = await self._stream_error_text(resp)
            self._raise_http_error(
                status_code,
                body,
                resource,
                getattr(resp, "headers", None),
            )
        return resp
