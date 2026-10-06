"""Firebase provider HTTP transport lifecycle.

Ownership contract: an injected (``set_http_client`` / constructor) client
is BORROWED and never closed by the provider; clients the provider builds
itself per resource are OWNED and closed by ``close()`` /
``invalidate_resource()``.
"""

from __future__ import annotations

import asyncio

import pytest

from core.models import ChatMessage, ChatRequest
from providers.firebase.provider import FirebaseProvider
from providers.firebase.resource import FirebaseResource


class FakeClient:
    """httpx.AsyncClient-shaped fake with aclose tracking."""

    def __init__(self, tag="client"):
        self.tag = tag
        self.closed = False
        self.calls: list = []

    async def aclose(self):
        self.closed = True

    async def post(self, url, json=None, headers=None):
        self.calls.append(url)
        return _json_response({"candidates": [], "usageMetadata": {}})

    async def get(self, url, headers=None):
        self.calls.append(url)
        return _json_response({})

    def stream(self, method, url, json=None, headers=None):
        raise AssertionError("stream not used in lifecycle tests")

    def build_url(self, *args):
        return "https://firebase.test"


class _JsonResponse:
    def __init__(self, body):
        self.status_code = 200
        self._body = body

    def json(self):
        return self._body


def _json_response(body):
    return _JsonResponse(body)


def _resource(resource_id="r1", proxy=None):
    payload = {"id": resource_id, "project_id": "p"}
    if proxy:
        payload["proxy"] = proxy
    return FirebaseResource.model_validate(payload)


_build_calls: list = []


def _fake_build_client(config):
    """Stub for transport.http.build_client: tagged fakes, no network."""
    client = FakeClient(f"owned-{len(_build_calls)}")
    _build_calls.append(client)
    return client


@pytest.fixture(autouse=True)
def _patch_build_client(monkeypatch):
    _build_calls.clear()
    monkeypatch.setattr("transport.http.build_client", _fake_build_client)


def _provider():
    provider = FirebaseProvider()
    # stub the credential store so no real credential resolution happens
    provider.set_credential_store(_EmptyStore())
    return provider


class _EmptyStore:
    def __contains__(self, credential_id):
        return True

    def get(self, credential_id):
        return None


def _request():
    return ChatRequest(
        model="gemini-3.8-flash",
        messages=[ChatMessage(role="user", content="hi")],
    )


def _owned_clients(provider):
    return dict(provider._owned_http)


# ---------------------------------------------------------------------------
# injected client: borrowed, never closed
# ---------------------------------------------------------------------------
async def test_injected_client_survives_provider_close():
    injected = FakeClient("injected")
    provider = FirebaseProvider(http_client=injected)
    provider.set_credential_store(_EmptyStore())
    await provider._client_for(_resource("r1"))  # builds adapter around it

    await provider.close()
    assert injected.closed is False  # borrowed: provider must NOT close it


# ---------------------------------------------------------------------------
# provider-owned clients: closed
# ---------------------------------------------------------------------------
async def test_resource_owned_client_closed_by_provider_close():
    provider = _provider()
    await provider._client_for(_resource("r1"))
    owned = _owned_clients(provider)
    assert set(owned) == {"r1"}  # ownership registered

    await provider.close()
    assert all(c.closed for c in owned.values())
    assert provider._owned_http == {}  # ownership cleared


async def test_multiple_resources_all_closed():
    provider = _provider()
    for rid in ("r1", "r2", "r3"):
        await provider._client_for(_resource(rid))
    owned = _owned_clients(provider)
    assert set(owned) == {"r1", "r2", "r3"}
    assert owned["r1"] is not owned["r2"]  # per-resource transports

    await provider.close()
    assert all(c.closed for c in owned.values())


async def test_close_is_idempotent_and_isolates_failures():
    provider = _provider()
    await provider._client_for(_resource("r1"))
    await provider._client_for(_resource("r2"))

    good, bad = list(provider._owned_http.values())
    async def failing_aclose():
        raise RuntimeError("close failed")
    bad.aclose = failing_aclose  # one client refuses to close

    await provider.close()  # must not raise
    assert good.closed is True  # the other client still got closed
    await provider.close()  # second close: no exception, nothing left
    assert provider._owned_http == {} and not provider._retired_http


# ---------------------------------------------------------------------------
# invalidation closes the owned client of exactly that resource
# ---------------------------------------------------------------------------
async def test_invalidate_closes_owned_client_of_that_resource_only():
    provider = _provider()
    await provider._client_for(_resource("r1"))
    await provider._client_for(_resource("r2"))
    r1_client = provider._owned_http["r1"]
    r2_client = provider._owned_http["r2"]

    await provider.invalidate_resource("r1")  # awaitable seam

    assert r1_client.closed is True
    assert r2_client.closed is False  # untouched resource unaffected
    assert "r1" not in provider._owned_http
    assert "r1" not in provider._clients
    assert "r1" not in provider._adapters
    # r2 still fully usable
    assert "r2" in provider._clients and "r2" in provider._adapters


async def test_invalidate_never_closes_borrowed_client():
    injected = FakeClient("injected")
    provider = FirebaseProvider(http_client=injected)
    provider.set_credential_store(_EmptyStore())
    await provider._client_for(_resource("r1"))
    assert provider._owned_http == {}  # borrowed -> no ownership

    await provider.invalidate_resource("r1")
    assert injected.closed is False


# ---------------------------------------------------------------------------
# sync re-wiring retires owned clients (drained by close)
# ---------------------------------------------------------------------------
async def test_set_http_client_retires_old_owned_clients():
    provider = _provider()
    await provider._client_for(_resource("r1"))
    old_owned = provider._owned_http["r1"]

    provider.set_http_client(FakeClient("injected"))  # sync re-wire
    assert provider._owned_http == {}  # ownership released
    assert old_owned not in provider._owned_http.values()

    await provider.close()  # retired client is drained and closed
    assert old_owned.closed is True


async def test_set_credential_store_retires_old_owned_clients():
    provider = _provider()
    await provider._client_for(_resource("r1"))
    old_owned = provider._owned_http["r1"]

    provider.set_credential_store(_EmptyStore())  # sync re-wire
    assert provider._owned_http == {}

    await provider.close()
    assert old_owned.closed is True
