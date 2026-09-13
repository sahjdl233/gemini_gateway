"""Gemini CLI (Code Assist) HTTP client.

Talks to cloudcode-pa.googleapis.com/v1internal.
Handles 401 -> force-refresh -> retry-once (Gateway enhancement, not in
gcli2api).  429/5xx are NOT retried here; they propagate as ProviderError
to the Scheduler which decides cooldown/failover.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Optional

import httpx

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
    """Thin HTTP client for the Code Assist internal API."""

    def __init__(self, http: Any, auth: GeminiCliAuth) -> None:
        self._http = http
        self._auth = auth

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
        """
        for attempt in range(2):
            token = await self._auth.get_access_token(
                resource, force=(attempt > 0)
            )
            headers = self._build_headers(resource, token)
            url = self._build_url(base_url, operation, streaming=streaming)
            try:
                resp = await self._http.post(url, headers=headers, json=payload)
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
        """Async generator yielding httpx stream responses (SSE body).

        On 401 the token is force-refreshed and the stream is retried once.
        """
        for attempt in range(2):
            token = await self._auth.get_access_token(
                resource, force=(attempt > 0)
            )
            headers = self._build_headers(resource, token)
            url = self._build_url(
                base_url, "streamGenerateContent", streaming=True
            )
            try:
                async with self._http.stream(
                    "POST", url, headers=headers, json=payload
                ) as resp:
                    if resp.status_code == 401 and attempt == 0:
                        await resp.aread()
                        logger.info(
                            "gemini_cli 401 retry (stream) resource=%s",
                            resource.id,
                        )
                        self._auth.invalidate()
                        continue
                    if resp.status_code != 200:
                        body = await resp.aread()
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
                    yield resp
                    return
            except (httpx.StreamError, httpx.HTTPError) as exc:
                raise classify_transport_error(
                    exc, resource_id=resource.id
                ) from exc
        raise classify_http_error(
            401,
            "gemini_cli: auth retry exhausted (stream)",
            resource_id=resource.id,
        )
