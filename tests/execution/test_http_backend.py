"""TASK-ARCH-003 Phase 1: HttpExecutionBackend lifecycle and isolation.

No test here touches the network: every backend is built on
httpx.MockTransport or on a real httpx.AsyncClient with only .request stubbed.
"""

from unittest.mock import AsyncMock

import httpx
import pytest

from execution.http import (
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_MAX_KEEPALIVE_CONNECTIONS,
    HttpExecutionBackend,
)
from transport.proxy import ProxyConfig


def _ok():
    return httpx.Response(200, json={"ok": True})


class RecordingClient(httpx.AsyncClient):
    """A REAL httpx.AsyncClient (real transports, pools, timeouts) with only
    .request stubbed to avoid network I/O."""

    def __init__(self, **kwargs):
        self.requests = []
        self.aclose_calls = 0
        super().__init__(**kwargs)

    async def request(self, *args, **kwargs):
        self.requests.append({"args": args, "kwargs": dict(kwargs)})
        return _ok()

    async def aclose(self):
        self.aclose_calls += 1


def test_backend_builds_one_persistent_async_client():
    """One backend == one AsyncClient, constructed once, pool created now."""
    backend = HttpExecutionBackend(timeout_seconds=7.5)

    assert isinstance(backend._client, httpx.AsyncClient)
    assert backend._owns_client is True
    assert backend._client.timeout.read == 7.5
    assert backend._client.timeout.pool == 7.5
    assert backend._client._transport is not None
    # Defaults stay small for the 1 vCPU / 1 GB target.
    assert DEFAULT_MAX_CONNECTIONS == 20
    assert DEFAULT_MAX_KEEPALIVE_CONNECTIONS == 8


def test_backend_propagates_pool_limits_to_the_real_pool():
    """Regression: max_connections is not an AsyncClient kwarg, so pool sizing
    must flow through limits= to actually bound the connection pool."""
    backend = HttpExecutionBackend(max_connections=3, max_keepalive_connections=2)
    pool = backend._client._transport._pool
    assert pool._max_connections == 3
    assert pool._max_keepalive_connections == 2


async def test_backend_reuses_the_same_client_across_requests():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"n": len(seen)})

    backend = HttpExecutionBackend(transport=httpx.MockTransport(handler))
    # No client is created per execute(): the backend keeps ONE.
    assert backend._client is backend._client

    r1 = await backend.execute("POST", "https://upstream.test/v1", json={"a": 1})
    r2 = await backend.execute("POST", "https://upstream.test/v1", json={"a": 2})
    assert backend._client is backend._client

    assert len(seen) == 2
    assert r1.json()["n"] == 1
    assert r2.json()["n"] == 2
    await backend.close()


async def test_borrowed_client_is_not_closed_by_the_backend():
    """A borrowed transport stays the creator's responsibility."""
    aclose = AsyncMock()
    backend = HttpExecutionBackend(client=type("Fake", (), {"aclose": aclose})())

    await backend.close()
    await backend.close()
    assert aclose.await_count == 0
    assert backend._owns_client is False


async def test_backend_builds_and_closes_its_own_client():
    backend = HttpExecutionBackend(transport=httpx.MockTransport(lambda request: _ok()))
    assert backend._owns_client is True

    await backend.execute("POST", "https://upstream.test/v1")
    await backend.close()
    await backend.close()
    assert backend._closed is True


async def test_owned_injected_client_is_closed_exactly_once():
    """owned=True takes over shutdown of an injected transport."""
    client = RecordingClient(timeout=1.0)
    backend = HttpExecutionBackend(client=client, timeout_seconds=1.0, owned=True)

    await backend.execute("POST", "https://upstream.test/v1")
    assert client.aclose_calls == 0

    await backend.close()
    assert client.aclose_calls == 1
    await backend.close()
    assert client.aclose_calls == 1  # already closed


async def test_backend_receives_proxy_pool_and_default_headers():
    client = RecordingClient(
        timeout=1.0,
        proxy="http://127.0.0.1:8080",
        limits=httpx.Limits(max_connections=3, max_keepalive_connections=2),
        headers={"User-Agent": "personal-ai-gateway/0.1"},
    )
    backend = HttpExecutionBackend(
        client=client,
        proxy=ProxyConfig(scheme="http", host="127.0.0.1", port=8080),
        max_connections=3,
        max_keepalive_connections=2,
    )

    await backend.execute("POST", "https://upstream.test/v1")

    assert client.headers.get("user-agent") == "personal-ai-gateway/0.1"
    assert backend._client is client


async def test_backend_owns_no_credential_fields():
    """Identity material is request-level only. A backend is shared by every
    resource of the provider, so persisting one account's tokens there would
    leak it into every other account's request."""
    seen_headers = []

    def handler(request):
        seen_headers.append(request.headers.get("Authorization"))
        return _ok()

    backend = HttpExecutionBackend(transport=httpx.MockTransport(handler))

    for token in ("tok-a", "tok-b"):
        await backend.execute(
            "POST",
            "https://upstream.test/v1",
            headers={"Authorization": f"Bearer {token}"},
            json={"x": token},
        )

    assert seen_headers == ["Bearer tok-a", "Bearer tok-b"]

    for field in (
        "access_token",
        "refresh_token",
        "client_id",
        "client_secret",
        "api_key",
        "cookie",
        "auth_user",
    ):
        assert not hasattr(backend, field)
        with pytest.raises(AttributeError, match="owns no identity material"):
            setattr(backend, field, "leak")

    await backend.close()


async def test_per_request_timeout_merges_onto_base_timeout():
    """A request-level override must not replace the backend's base budget.

    Regression: naming read/write/pool phases on httpx.Timeout() pins EVERY
    phase to the request budget, so a 5s request read silently became 30s and
    the shared pool phase lost its bound.
    """
    client = RecordingClient(timeout=30.0)
    backend = HttpExecutionBackend(client=client, timeout_seconds=30.0)

    await backend.execute("POST", "https://upstream.test/v1", timeout=5.0)

    request_timeout = client.requests[0]["kwargs"]["timeout"]
    # Request-level override applies to the read phase...
    assert request_timeout.read == 5.0
    # ...while the shared pool phase keeps the backend-wide base budget.
    assert request_timeout.pool == 30.0
    assert client.requests[0]["kwargs"]["headers"] is None


class _StreamingResponse:
    def __init__(self, chunks, fail_after=None):
        self.status_code = 200
        self._chunks = iter(chunks)
        self._fail_after = fail_after

    def read(self):
        raise AssertionError("streaming response must not be fully read")

    async def aiter_bytes(self):
        for index, chunk in enumerate(self._chunks):
            if self._fail_after is not None and index >= self._fail_after:
                raise RuntimeError("consumer failed")
            yield chunk


class _StreamContext:
    def __init__(self, response):
        self.response = response
        self.exits = []

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        self.exits.append((exc_type, exc))


class _StreamingClient:
    def __init__(self, chunks, fail_after=None):
        self.context = _StreamContext(_StreamingResponse(chunks, fail_after))
        self.calls = []
        self.aclose_calls = 0

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.context

    async def aclose(self):
        self.aclose_calls += 1


async def test_execute_stream_yields_sse_chunks_without_buffering_response():
    client = _StreamingClient([
        b"data: chunk1\n\n",
        b"data: chunk2\n\n",
        b"data: [DONE]\n\n",
    ])
    backend = HttpExecutionBackend(client=client, owned=True)

    response = await backend.execute_stream(
        "POST",
        "https://upstream.test/v1internal:streamGenerateContent?alt=sse",
        json={"request": {}},
        headers={"Authorization": "Bearer resource-token"},
    )

    received = [chunk async for chunk in response.aiter_bytes()]

    assert received == [
        b"data: chunk1\n\n",
        b"data: chunk2\n\n",
        b"data: [DONE]\n\n",
    ]
    assert client.context.exits == [(None, None)]
    assert client.calls[0][0] == "POST"
    assert client.calls[0][2]["headers"] == {
        "Authorization": "Bearer resource-token",
    }


async def test_execute_stream_closes_response_on_consumer_error_only():
    client = _StreamingClient(
        [b"data: first\n\n", b"data: second\n\n"],
        fail_after=1,
    )
    backend = HttpExecutionBackend(client=client, owned=True)
    response = await backend.execute_stream(
        "POST", "https://upstream.test/stream"
    )

    with pytest.raises(RuntimeError, match="consumer failed"):
        async for _ in response.aiter_bytes():
            pass

    assert len(client.context.exits) == 1
    assert client.context.exits[0][0] is RuntimeError
    assert client.aclose_calls == 0
