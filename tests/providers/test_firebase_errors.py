"""TASK-004 tests: upstream HTTP errors -> core ProviderError hierarchy.

Every error path must land in core.errors.ProviderError (never a raw
httpx.HTTPStatusError). 429 carries scope="resource" + Retry-After so the
core Scheduler/Cooldown can honour it.
"""
from __future__ import annotations

import pytest

from core.errors import (
    AuthenticationError,
    AuthorizationError,
    InvalidRequestError,
    ModelNotFoundError,
    ProviderError,
    RateLimitError,
    UpstreamUnavailableError,
)

from providers.firebase.client import FirebaseClient
from providers.firebase.errors import (
    classify_http_error,
    extract_retry_after,
    FirebaseAuthError,
    FirebaseNetworkError,
    FirebaseRateLimitError,
    FirebaseTimeoutError,
    FirebaseUnavailableError,
)

from tests.providers._firebase_fakes import FakeHttp, FakeResponse

PROVIDER = "firebase"


def error_body(message="boom"):
    return ('{"error": {"message": "%s"}}' % message).encode()


def test_400_maps_to_invalid_request():
    err = classify_http_error(400, error_body("bad request"))
    assert isinstance(err, InvalidRequestError)
    assert isinstance(err, ProviderError)


def test_401_maps_to_authentication():
    err = classify_http_error(401, error_body("invalid api key"))
    assert isinstance(err, AuthenticationError)
    assert isinstance(err, FirebaseAuthError)


def test_403_maps_to_authorization():
    err = classify_http_error(403, error_body("forbidden"))
    assert isinstance(err, AuthorizationError)


def test_404_maps_to_model_not_found():
    err = classify_http_error(404, error_body("model not found"))
    assert isinstance(err, ModelNotFoundError)


def test_429_maps_to_rate_limit_with_scope_and_retry_after():
    err = classify_http_error(429, error_body("quota exceeded"), retry_after=12.5)
    assert isinstance(err, RateLimitError)
    assert isinstance(err, FirebaseRateLimitError)
    assert err.scope == "resource"
    assert err.retry_after == 12.5
    assert err.provider == PROVIDER


def test_5xx_maps_to_upstream_unavailable():
    for code in (500, 502, 503):
        err = classify_http_error(code, error_body("upstream down"))
        assert isinstance(err, UpstreamUnavailableError)
        assert isinstance(err, FirebaseUnavailableError)


def test_extract_retry_after_header():
    resp = FakeResponse(429, headers={"Retry-After": "30"})
    assert extract_retry_after(resp) == 30.0


def test_extract_retry_after_missing():
    resp = FakeResponse(429)
    assert extract_retry_after(resp) is None


def test_all_errors_are_provider_errors():
    for code in (400, 401, 403, 404, 429, 500, 503):
        err = classify_http_error(code, error_body("x"))
        assert isinstance(err, ProviderError)


async def test_client_complete_429_raises_rate_limit():
    fake = FakeHttp()
    client = FirebaseClient(http=fake, auth=_noop_auth(fake))
    fake.responses.append(
        FakeResponse(429, headers={"Retry-After": "7"}, content=error_body("quota"))
    )
    with pytest.raises(RateLimitError) as ei:
        await client.complete(_resource(), "gemini-3.8-flash", {"contents": []})
    assert ei.value.retry_after == 7.0
    assert ei.value.scope == "resource"


async def test_client_complete_401_then_success():
    fake = FakeHttp()
    auth = _auth_class(fake)
    client = FirebaseClient(http=fake, auth=auth)
    # auth exchange 200, then AI 401 (forces refresh), then AI 200
    fake.responses.append(fake.exchange_ok(token="jwt-1"))
    fake.responses.append(
        FakeResponse(401, content=error_body("expired token"))
    )
    fake.responses.append(fake.exchange_ok(token="jwt-2"))
    fake.responses.append(FakeResponse(200, json_body={"candidates": []}))
    resp = await client.complete(_resource(), "gemini-3.8-flash", {"contents": []})
    assert resp.status_code == 200
    assert len(fake.post_calls) == 4  # exchange + 401 + exchange + success


async def test_client_complete_timeout_is_provider_error():
    fake = FakeHttp()
    client = FirebaseClient(http=fake, auth=_noop_auth(fake))
    fake.raise_exc = TimeoutError("timed out")
    with pytest.raises(ProviderError) as ei:
        await client.complete(_resource(), "gemini-3.8-flash", {"contents": []})
    assert isinstance(ei.value, FirebaseTimeoutError)


async def test_client_complete_connection_error_is_provider_error():
    fake = FakeHttp()
    client = FirebaseClient(http=fake, auth=_noop_auth(fake))
    fake.raise_exc = ConnectionError("connection refused")
    with pytest.raises(ProviderError) as ei:
        await client.complete(_resource(), "gemini-3.8-flash", {"contents": []})
    assert isinstance(ei.value, FirebaseNetworkError)


def _resource():
    from tests.providers._firebase_fakes import make_resource

    return make_resource()


class _NoopAuth:
    async def get_jwt(self, *args, **kwargs):
        return "jwt"

    def invalidate(self):
        pass


def _noop_auth(fake):
    return _NoopAuth()


def _auth_class(fake):
    from providers.firebase.auth import FirebaseAuth

    return FirebaseAuth(client=fake)

