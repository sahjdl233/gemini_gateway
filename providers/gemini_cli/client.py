"""Gemini CLI (Code Assist) HTTP client.

Talks to cloudcode-pa.googleapis.com/v1internal.
Handles 401 -> force-refresh -> retry-once (Gateway enhancement, not in
gcli2api).  429/5xx are NOT retried here; they propagate as ProviderError
to the Scheduler which decides cooldown/failover.

TASK-ARCH-004: this class owns no transport.  It depends on the
provider-owned ``ExecutionBackend``, which supplies ONE persistent
AsyncClient shared by every GeminiCliResource, so transport is an
O(provider) resource instead of an O(resource) one.  The client keeps the
protocol responsibilities: URL shape, headers, Code Assist payloads,
401 -> invalidate -> force refresh -> retry once, and the 429/5xx/network
error classification.  Auth is resolved per request from the selected
Resource and travels as a request header only.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Optional

import httpx

from execution.base import ExecutionBackend
from providers.gemini_cli.auth import GeminiCliAuth
from providers.gemini_cli.errors import (
    classify_http_error,
    classify_transport_error,
)
from providers.gemini_cli.resource import GeminiCliResource

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://cloudcode-pa.googleapis.com"
_USER_AGENT = (
    "Mozilla/5.0 (compatible; Google-Gemini-CLI/1.0; "
    "https://github.com/google-gemini/gemini-cli)"
)


class GeminiCliClient:
    """Thin protocol client for the Code Assist internal API.

    ``backend`` is required: a missing backend is a wiring error and must
    fail loudly rather than silently rebuilding a per-resource transport.
    """

    def __init__(self, backend: ExecutionBackend, auth: GeminiCliAuth) -> None:
        self._backend = backend
        self._auth = auth

    @property
    def backend(self) -> ExecutionBackend:
        """The shared ExecutionBackend this client issues requests through."""
        return self._backend

    def _require_backend(self) -> ExecutionBackend:
        if self._backend is None:
            raise RuntimeError(
                "GeminiCliClient needs a provider-owned ExecutionBackend; "
                "the per-resource httpx client path has been removed."
            )
        return self._backend

    def _build_headers(
        self,
        resource: GeminiCliResource,
        access_token: str,
    ) -> dict:
        return {
            "Authorization": "Bearer " + access_token,
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }

    def _build_url(
        self,
        base_url: str,
        operation: str,
        streaming: bool = False,
    ) -> str:
        path = "/v1internal:" + operation
        if streaming:
            path += "?alt=sse"
        return base_url.rstrip("/") + path

    @staticmethod
    async def _drain(resp: Any) -> str:
        """Read a bounded error body from a live stream response.

        The response context is always released afterwards, so a 401 retry
        or a non-200 classification never leaks an open upstream response.
        """
        parts: list[str] = []
        try:
            async for chunk in resp.aiter_bytes():
                if not chunk:
                    continue
                parts.append(
                    chunk.decode("utf-8", errors="replace")
                    if isinstance(chunk, bytes)
                    else str(chunk)
                )
                if sum(len(p) for p in parts) >= 2000:
                    break
        finally:
            await resp.aclose()
        return "".join(parts)[:2000]

    # -- non-streaming complete ---------------------------------------------

    async def post(
        self,
        resource: GeminiCliResource,
        base_url: str,
        payload: dict,
        *,
        streaming: bool = False,
        operation: str = "generateContent",
    ) -> Any:
        """POST to the given operation on the Code Assist internal API.

        Default operation is "generateContent".
        On 401 the token is force-refreshed and the call is retried once.

        The request travels through the provider-owned ExecutionBackend.
        The backend never retries, so the 401 retry below and the
        429/5xx propagation to the Scheduler stay exactly as before.
        """
        backend = self._require_backend()
        for attempt in range(2):
            token = await self._auth.get_access_token(
                resource, force=(attempt > 0)
            )
            headers = self._build_headers(resource, token)
            url = self._build_url(base_url, operation, streaming=streaming)
            try:
                resp = await backend.execute(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                )
            except Exception as exc:  # noqa: BLE001
                raise classify_transport_error(
                    exc,
                    resource_id=resource.id,
                ) from exc
            if resp.status_code == 401 and attempt == 0:
                logger.info("gemini_cli 401 retry resource=%s", resource.id)
                self._auth.invalidate()
                continue
            if resp.status_code != 200:
                body = getattr(resp, "content", b"")
                if isinstance(body, bytes):
                    try:
                        body = body.decode("utf-8", errors="replace")
                    except Exception:  # noqa: BLE001
                        body = str(body)[:500]
                raise classify_http_error(
                    resp.status_code,
                    body,
                    resource_id=resource.id,
                )
            return resp
        raise classify_http_error(
            401,
            "gemini_cli: auth retry exhausted",
            resource_id=resource.id,
        )

    # -- streaming ----------------------------------------------------------

    async def stream(
        self,
        resource: GeminiCliResource,
        base_url: str,
        payload: dict,
    ) -> AsyncIterator[Any]:
        """Async generator yielding the backend's live stream response.

        On 401 the token is force-refreshed and the stream is retried once.

        The response comes from ``backend.execute_stream()`` as a
        ``StreamResponse``: the upstream body stays incrementally readable
        and the *response* context is released exactly once, on normal
        completion and when the consumer raises mid-iteration.  The
        provider-level shared AsyncClient is never closed here, so the
        backend stays reusable for the next request.
        """
        backend = self._require_backend()
        for attempt in range(2):
            token = await self._auth.get_access_token(
                resource, force=(attempt > 0)
            )
            headers = self._build_headers(resource, token)
            url = self._build_url(
                base_url, "streamGenerateContent", streaming=True
            )
            resp = None
            try:
                resp = await backend.execute_stream(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                )
                if resp.status_code == 401 and attempt == 0:
                    await self._drain(resp)
                    logger.info(
                        "gemini_cli 401 retry (stream) resource=%s",
                        resource.id,
                    )
                    self._auth.invalidate()
                    continue
                if resp.status_code != 200:
                    body = await self._drain(resp)
                    raise classify_http_error(
                        resp.status_code,
                        body,
                        resource_id=resource.id,
                    )
                yield resp
                return
            except (httpx.StreamError, httpx.HTTPError) as exc:
                raise classify_transport_error(
                    exc, resource_id=resource.id
                ) from exc
            finally:
                # Release the RESPONSE context exactly once, on every exit:
                # 401 retry, non-200 raise, normal completion, consumer
                # exception, or an abandoned stream.  StreamResponse.aclose
                # is idempotent, so a fully consumed body is a no-op here.
                # The shared AsyncClient behind the backend is untouched.
                if resp is not None:
                    await resp.aclose()
        raise classify_http_error(
            401,
            "gemini_cli: auth retry exhausted (stream)",
            resource_id=resource.id,
        )
