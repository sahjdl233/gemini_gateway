"""Async HTTP transport for future Google providers.

TASK-000 does NOT make any real HTTP request to Google.  This module
only shapes how later provider adapters will talk over the wire, keeping
proxy/egress fully decoupled from provider logic.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from .proxy import TransportConfig


def build_client(config: TransportConfig) -> httpx.AsyncClient:
    proxy_url = config.proxy.as_url() if config.proxy else None
    return httpx.AsyncClient(
        proxy=proxy_url,
        timeout=config.timeout_seconds,
        verify=config.verify_tls,
        follow_redirects=config.follow_redirects,
        headers=config.extra_headers,
    )


async def fetch_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    response = await client.request(method, url, params=params, json=json_body)
    return response.json()
