from __future__ import annotations

from typing import Any, Mapping, Optional

import logging

from execution.base import ExecutionBackend
from core.errors import RateLimitError

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
            if status_code == 429:
                raise RateLimitError(
                    "Antigravity upstream rate-limited (429)",
                    provider="antigravity",
                )
            raise RuntimeError(
                "Antigravity upstream error HTTP %s: %s",
                status_code,
                resp.text[:512],
            )
        return resp.json()

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
            if status_code == 429:
                raise RateLimitError(
                    "Antigravity upstream rate-limited (429)",
                    provider="antigravity",
                )
            raise RuntimeError(
                "Antigravity upstream error HTTP %s: %s",
                status_code,
                resp.text[:512],
            )
        return resp
