"""Firebase App Check auth tests (TASK-004)."""
from __future__ import annotations

import time

import pytest

from core.errors import ProviderError
from providers.firebase.auth import FirebaseAuth
from providers.firebase.errors import (
    FirebaseAuthError,
    FirebaseNetworkError,
    FirebaseTimeoutError,
)

from tests.providers._firebase_fakes import FakeHttp, FakeResponse


def make_auth(fake: FakeHttp) -> FirebaseAuth:
    return FirebaseAuth(client=fake)


async def test_first_fetch_exchanges_debug_token(fake_http):
    auth = make_auth(fake_http)
    fake_http.responses.append(fake_http.exchange_ok(token="jwt-1", ttl="3600s"))

    jwt = await auth.get_jwt("proj", "app", "key", "debug")

    assert jwt == "jwt-1"
    assert len(fake_http.post_calls) == 1
    call = fake_http.post_calls[0]
    assert "exchangeDebugToken" in call["url"]
    assert call["url"].startswith(
        "https://firebaseappcheck.googleapis.com/v1/projects/proj"
        "/apps/app:exchangeDebugToken"
    )
    assert call["headers"]["x-goog-api-key"] == "key"
    assert call["json"] == {"debug_token": "debug", "limited_use": False}


async def test_jwt_cache_reuses_token(fake_http):
    auth = make_auth(fake_http)
    fake_http.responses.append(fake_http.exchange_ok(token="jwt-1"))

    first = await auth.get_jwt("proj", "app", "key", "debug")
    second = await auth.get_jwt("proj", "app", "key", "debug")

    assert first == second == "jwt-1"
    assert len(fake_http.post_calls) == 1  # cached, no second exchange


async def test_refresh_when_near_expiry(fake_http):
    auth = make_auth(fake_http)
    fake_http.responses.append(fake_http.exchange_ok(token="jwt-a", ttl="3600s"))
    assert await auth.get_jwt("proj", "app", "key", "debug") == "jwt-a"

    # Simulate a token that expires in 100s (< PRE_REFRESH_SECONDS=300).
    auth._jwt_exp = time.time() + 100

    fake_http.responses.append(fake_http.exchange_ok(token="jwt-b", ttl="3600s"))
    assert await auth.get_jwt("proj", "app", "key", "debug") == "jwt-b"
    assert len(fake_http.post_calls) == 2


async def test_force_refresh(fake_http):
    auth = make_auth(fake_http)
    fake_http.responses.append(fake_http.exchange_ok(token="jwt-a"))
    await auth.get_jwt("proj", "app", "key", "debug")

    fake_http.responses.append(fake_http.exchange_ok(token="jwt-b"))
    jwt = await auth.get_jwt("proj", "app", "key", "debug", force=True)

    assert jwt == "jwt-b"
    assert len(fake_http.post_calls) == 2


async def test_invalidate_forces_next_fetch(fake_http):
    auth = make_auth(fake_http)
    fake_http.responses.append(fake_http.exchange_ok(token="jwt-a"))
    await auth.get_jwt("proj", "app", "key", "debug")

    auth.invalidate()
    fake_http.responses.append(fake_http.exchange_ok(token="jwt-b"))
    jwt = await auth.get_jwt("proj", "app", "key", "debug")

    assert jwt == "jwt-b"
    assert len(fake_http.post_calls) == 2


async def test_exchange_failure_raises_provider_error(fake_http):
    auth = make_auth(fake_http)
    fake_http.responses.append(
        FakeResponse(401, content=b'{"error": {"message": "bad debug token"}}')
    )
    with pytest.raises(FirebaseAuthError):
        await auth.get_jwt("proj", "app", "key", "debug")


async def test_exchange_network_error_is_provider_error(fake_http):
    auth = make_auth(fake_http)
    fake_http.raise_exc = RuntimeError("connection refused")
    with pytest.raises(FirebaseNetworkError):
        await auth.get_jwt("proj", "app", "key", "debug")


async def test_exchange_timeout_is_provider_error(fake_http):
    auth = make_auth(fake_http)
    fake_http.raise_exc = TimeoutError("timed out")
    with pytest.raises(FirebaseTimeoutError):
        await auth.get_jwt("proj", "app", "key", "debug")


async def test_error_is_core_provider_error_hierarchy(fake_http):
    auth = make_auth(fake_http)
    fake_http.responses.append(
        FakeResponse(403, content=b'{"error": {"message": "denied"}}')
    )
    with pytest.raises(ProviderError) as ei:
        await auth.get_jwt("proj", "app", "key", "debug")
    assert isinstance(ei.value, FirebaseAuthError)


# ---------------------------------------------------------------------------
# Shared fixture re-declared here for autouse-less clarity.
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_http():
    return FakeHttp()

