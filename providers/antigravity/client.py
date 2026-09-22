from __future__ import annotations

from typing import Any, Mapping

import httpx

from .resource import AntigravityResource

BASE_URL = "https://daily-cloudcode-pa.googleapis.com"


class AntigravityClient:
    def __init__(self, *, timeout: float = 30.0, client: httpx.Client | None = None) -> None:
        self.timeout = timeout
        self._client = client

    def _request(
        self,
        operation: str,
        resource: AntigravityResource,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        endpoint = f"{BASE_URL}/v1internal:{operation}"
        headers = {"Content-Type": "application/json"}
        if resource.access_token:
            headers["Authorization"] = f"Bearer {resource.access_token}"

        if self._client is not None:
            resp = self._client.post(endpoint, json=payload or {}, headers=headers, timeout=self.timeout)
        else:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(endpoint, json=payload or {}, headers=headers)

        status_code = resp.status_code
        if status_code >= 400:
            raise RuntimeError(f"Antigravity request failed for {operation}: HTTP {status_code}: {resp.text}")

        return resp.json()

    def fetch_available_models(
        self,
        resource: AntigravityResource,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        return self._request("fetchAvailableModels", resource, payload)