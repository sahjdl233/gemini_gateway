"""Shared fake HTTP objects for Firebase provider tests (not collected).

Mimics the subset of httpx.AsyncClient the FirebaseClient/Auth expect:
  - post(url, headers=..., json=...) -> response
  - stream(method, url, headers=..., json=...) -> async context manager

The response object exposes status_code / json() / content / headers / aread()
/ aiter_bytes() and supports async-with, matching httpx semantics.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def make_sse(*events: Any) -> bytes:
    """Build an SSE body from raw events; pass the string "[DONE]" for the
    terminal marker."""
    parts: List[str] = []
    for e in events:
        if e == "[DONE]":
            parts.append("data: [DONE]")
        else:
            parts.append("data: " + json.dumps(e, ensure_ascii=False))
    return ("\n\n".join(parts) + "\n\n").encode("utf-8")


class FakeResponse:
    """Response-like object compatible with httpx expectations."""

    def __init__(
        self,
        status_code: int = 200,
        json_body: Optional[Any] = None,
        content: bytes = b"",
        headers: Optional[Dict[str, str]] = None,
        sse_chunks: Optional[List[bytes]] = None,
    ) -> None:
        self.status_code = status_code
        self._json = json_body
        self.content = content
        self.headers = headers or {}
        self._sse_chunks = sse_chunks

    def json(self) -> Any:
        return self._json

    async def aread(self) -> bytes:
        return self.content

    async def aiter_bytes(self) -> Any:
        for chunk in self._sse_chunks or [self.content]:
            if chunk:
                yield chunk

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeHttp:
    """AsyncClient-compatible fake honouring post() and stream().

    Responses are popped FIFO: for a normal complete() call you should
    queue [exchange_response, ai_response]; for stream() queue
    [exchange_response, ai_stream_response].
    """

    def __init__(self) -> None:
        self.post_calls: List[dict] = []
        self.stream_calls: List[dict] = []
        self.responses: List[FakeResponse] = []
        self.stream_responses: List[FakeResponse] = []
        self.raise_exc: Optional[Exception] = None

    async def post(self, url, *, headers=None, json=None) -> FakeResponse:
        self.post_calls.append({"url": url, "headers": headers, "json": json})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.responses.pop(0)

    def stream(self, method, url, *, headers=None, json=None) -> FakeResponse:
        # httpx.AsyncClient.stream is a non-async context manager factory in
        # httpx (it returns an async context manager), so we intentionally
        # keep this synchronous.
        self.stream_calls.append(
            {"method": method, "url": url, "headers": headers, "json": json}
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.stream_responses.pop(0)

    def exchange_ok(self, token: str = "jwt-token-1", ttl: str = "3600s") -> FakeResponse:
        return FakeResponse(200, json_body={"token": token, "ttl": ttl})


def make_resource(**overrides) -> Any:
    """Build a FirebaseResource with sensible defaults."""
    from providers.firebase.resource import FirebaseResource

    base = {
        "id": "firebase-project-01",
        "provider": "firebase",
        "project_id": "test-project",
        "api_key": "AIzaSyTESTAPIKEY",
        "app_id": "1:12345:web:abc123",
        "debug_token": "debug-token-0000",
    }
    base.update(overrides)
    return FirebaseResource.model_validate(base)

