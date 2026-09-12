"""Shared fake HTTP objects for Gemini CLI provider tests (not collected).

Mimics the subset of httpx.AsyncClient that GeminiCliClient / GeminiCliAuth
expect:
  - post(url, headers=..., json=... or data=...) -> response
  - stream(method, url, headers=..., json=...) -> async context manager

The response exposes status_code / json() / content / headers
/ aiter_bytes() / aread() and supports async-with, like httpx.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def make_sse(*events: Any) -> bytes:
    parts: List[str] = []
    for e in events:
        if e == "[DONE]":
            parts.append("data: [DONE]")
        else:
            parts.append("data: " + json.dumps(e, ensure_ascii=False))
    return ("\n\n".join(parts) + "\n\n").encode("utf-8")


def envelope(inner: Dict[str, Any], trace: str = "trace-1") -> Dict[str, Any]:
    return {"response": inner, "traceId": trace}


def text_part(text: str, **extra: Any) -> Dict[str, Any]:
    part: Dict[str, Any] = {"text": text}
    part.update(extra)
    return part


def function_call_part(name: str, args: Optional[dict] = None) -> Dict[str, Any]:
    return {"functionCall": {"name": name, "args": args or {}}}


class FakeResponse:
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
        self.text_value: Optional[str] = None
        if isinstance(json_body, dict):
            self.text_value = json.dumps(json_body, ensure_ascii=False)
        elif isinstance(json_body, str):
            self.text_value = json_body

    @property
    def text(self) -> str:
        return self.text_value or self.content.decode("utf-8", errors="replace")

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
    def __init__(self) -> None:
        self.post_calls: List[dict] = []
        self.stream_calls: List[dict] = []
        self.responses: List[FakeResponse] = []
        self.stream_responses: List[FakeResponse] = []
        self.raise_exc: Optional[Exception] = None

    async def post(self, url, *, headers=None, json=None, data=None) -> FakeResponse:
        self.post_calls.append(
            {"url": url, "headers": headers, "json": json, "data": data}
        )
        if self.raise_exc is not None:
            exc = self.raise_exc
            self.raise_exc = None
            raise exc
        return self.responses.pop(0)

    def stream(self, method, url, *, headers=None, json=None) -> FakeResponse:
        self.stream_calls.append(
            {"method": method, "url": url, "headers": headers, "json": json}
        )
        if self.raise_exc is not None:
            exc = self.raise_exc
            self.raise_exc = None
            raise exc
        return self.stream_responses.pop(0)

    def token_ok(self, token: str = "access-token-1", expires_in: int = 3600) -> FakeResponse:
        return FakeResponse(
            200,
            json_body={"access_token": token, "expires_in": expires_in},
        )

    def ok(self, json_body: Any) -> FakeResponse:
        return FakeResponse(200, json_body=json_body)

    def err(self, status: int, body: Any, headers: Optional[Dict[str, str]] = None) -> FakeResponse:
        if isinstance(body, dict):
            content = json.dumps(body).encode("utf-8")
        elif isinstance(body, bytes):
            content = body
        else:
            content = str(body).encode("utf-8")
        return FakeResponse(status, json_body=body, content=content, headers=headers)


def make_resource(**overrides: Any) -> Any:
    from providers.gemini_cli.resource import GeminiCliResource

    base = {
        "id": "cli-account-01",
        "provider": "gemini_cli",
        "access_token": "access-token-1",
        "refresh_token": "refresh-token-1",
        "client_id": "client-id-1",
        "client_secret": "client-secret-1",
        "project_id": "gen-lang-client-test",
        "tier": "PRO",
    }
    base.update(overrides)
    return GeminiCliResource.model_validate(base)

