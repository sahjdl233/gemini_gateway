"""Firebase AI Logic HTTP client.

Constructs URLs, headers, and dispatches requests. Handles one 401 retry
with a forced JWT refresh (TASK-003: the App Check JWT is short-lived; a
stale token surfaces as 401, refresh once and retry). Rate limiting and
cooldown belong to the core Scheduler/Cooldown layer -- this client never
retries on 429/5xx on its own.

The streaming path uses httpx's async context manager properly
(async with client.stream(...) as response), then hands the SSE body to
the FirebaseStreamParser. Non-2xx upstream responses are classified into
the core ProviderError hierarchy (never surfaced as raw HTTP errors).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Dict, Optional

from core.errors import ProviderError

from providers.firebase.auth import FirebaseAuth
from providers.firebase.errors import (
    FirebaseNetworkError,
    FirebaseTimeoutError,
    classify_http_error,
    extract_retry_after,
)
from providers.firebase.resource import FirebaseResource
from providers.firebase.streaming import iter_sse_events

logger = logging.getLogger(__name__)

GEN_URL = "https://firebasevertexai.googleapis.com/v1beta"

# TASK-003 confirmed client identifier the Firebase SDK sends.
GOOG_API_CLIENT = "gl-js/@firebase/ai/2.15.0 fire/2.15.0"


class FirebaseClient:
    """Thin HTTP client for Firebase AI Logic."""

    def __init__(self, http: Any, auth: FirebaseAuth) -> None:
        self._http = http
        self._auth = auth

    def _build_url(
        self, resource: FirebaseResource, model: str, streaming: bool
    ) -> str:
        base = f"{GEN_URL}/projects/{resource.project_id}/models/{model}"
        if streaming:
            return f"{base}:streamGenerateContent?alt=sse"
        return f"{base}:generateContent"

    def _build_headers(
        self, resource: FirebaseResource, jwt: str
    ) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-goog-api-client": GOOG_API_CLIENT,
            "x-goog-api-key": resource.api_key,
            "X-Firebase-Appid": resource.app_id,
            "X-Firebase-AppCheck": jwt,
        }

    async def _post_with_retry(
        self,
        resource: FirebaseResource,
        model: str,
        payload: Dict[str, Any],
    ) -> Any:
        """Non-streaming POST; retry once on 401 with forced JWT refresh."""
        for attempt in (0, 1):
            jwt = await self._auth.get_jwt(
                resource.project_id,
                resource.app_id,
                resource.api_key,
                resource.debug_token,
                force=(attempt == 1),
            )
            headers = self._build_headers(resource, jwt)
            url = self._build_url(resource, model, streaming=False)
            try:
                resp = await self._http.post(url, headers=headers, json=payload)
            except Exception as exc:
                raise self._classify_transport_error(exc) from exc
            if resp.status_code == 401 and attempt == 0:
                self._auth.invalidate()
                continue
            return resp
        return resp  # unreachable; kept for type checkers

    async def complete(
        self,
        resource: FirebaseResource,
        model: str,
        payload: Dict[str, Any],
    ) -> Any:
        """Non-streaming request; return the raw response on 200."""
        resp = await self._post_with_retry(resource, model, payload)
        if resp.status_code != 200:
            retry_after = extract_retry_after(resp)
            body = resp.content if hasattr(resp, "content") else b""
            raise classify_http_error(resp.status_code, body, retry_after)
        return resp

    async def stream(
        self,
        resource: FirebaseResource,
        model: str,
        payload: Dict[str, Any],
    ) -> AsyncIterator[Dict[str, Any]]:
        """Streaming request; yield SSE-parsed JSON events.

        401 on the first attempt triggers a forced JWT refresh and one retry.
        All other non-2xx responses are classified into ProviderError.
        """
        for attempt in (0, 1):
            jwt = await self._auth.get_jwt(
                resource.project_id,
                resource.app_id,
                resource.api_key,
                resource.debug_token,
                force=(attempt == 1),
            )
            headers = self._build_headers(resource, jwt)
            url = self._build_url(resource, model, streaming=True)
            try:
                async with self._http.stream(
                    "POST", url, headers=headers, json=payload
                ) as resp:
                    if resp.status_code == 401:
                        self._auth.invalidate()
                        if attempt == 0:
                            continue
                        # Second 401: JWT refresh did not help; surface it.
                        retry_after = extract_retry_after(resp)
                        body = await resp.aread()
                        raise classify_http_error(401, body, retry_after)
                    if resp.status_code != 200:
                        retry_after = extract_retry_after(resp)
                        body = await resp.aread()
                        raise classify_http_error(
                            resp.status_code, body, retry_after
                        )
                    async for event in iter_sse_events(resp):
                        yield event
                    return
            except ProviderError:
                raise
            except Exception as exc:
                raise self._classify_transport_error(exc) from exc

    @staticmethod
    def _classify_transport_error(exc: Exception) -> ProviderError:
        msg = str(exc)[:200] or "network error"
        lowered = msg.lower()
        if (
            "timeout" in lowered
            or "timed out" in lowered
            or isinstance(exc, (asyncio.TimeoutError, TimeoutError))
        ):
            return FirebaseTimeoutError(msg, provider="firebase")
        return FirebaseNetworkError(msg, provider="firebase")
