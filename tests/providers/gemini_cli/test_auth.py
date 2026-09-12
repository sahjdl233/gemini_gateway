"""OAuth cache / refresh / 401 retry (TASK-008)."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from providers.gemini_cli.auth import GeminiCliAuth, MAX_REFRESH_ATTEMPTS
from providers.gemini_cli.errors import GeminiCliAuthError, GeminiCliNetworkError
from tests.providers._gemini_cli_fakes import FakeHttp, make_resource


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.value = start

    def time(self) -> float:
        return self.value


async def test_access_token_cached_within_expiry():
    http = FakeHttp()
    http.responses.append(http.token_ok("token-A", 3600))
    clock = FakeClock()
    auth = GeminiCliAuth(http, clock=clock)
    resource = make_resource()

    tok1 = await auth.get_access_token(resource)
    tok2 = await auth.get_access_token(resource)
    assert tok1 == "token-A"
    assert tok2 == "token-A"
    # Only one refresh call happened.
    assert len(http.post_calls) == 1


async def test_refresh_before_expiry_window():
    http = FakeHttp()
    http.responses.append(http.token_ok("token-A", 3600))
    http.responses.append(http.token_ok("token-B", 3600))
    clock = FakeClock()
    auth = GeminiCliAuth(http, clock=clock)
    resource = make_resource()

    tok1 = await auth.get_access_token(resource)
    assert tok1 == "token-A"

    # Jump past the 3-minute pre-refresh window (expiry at 4600).
    clock.value = 4300 + 180  # exactly at expiry threshold
    tok2 = await auth.get_access_token(resource)
    assert tok2 == "token-B"
    assert len(http.post_calls) == 2


async def test_force_refresh_ignores_cache():
    http = FakeHttp()
    http.responses.append(http.token_ok("token-A", 3600))
    http.responses.append(http.token_ok("token-B", 3600))
    auth = GeminiCliAuth(http, clock=FakeClock())
    resource = make_resource()

    await auth.get_access_token(resource)
    tok = await auth.get_access_token(resource, force=True)
    assert tok == "token-B"


async def test_401_force_refresh_retry_once():
    """client.post() on 401 invalidates and retries with force refresh."""
    from providers.gemini_cli.client import GeminiCliClient

    http = FakeHttp()
    # 1) refresh token inside auth -> token-A
    http.responses.append(http.token_ok("token-A", 3600))
    # 2) first API call -> 401
    http.responses.append(http.err(401, {"error": {"message": "invalid token"}}))
    # 3) force refresh -> token-B
    http.responses.append(http.token_ok("token-B", 3600))
    # 4) retried API call -> 200
    http.responses.append(
        http.ok({"response": {"candidates": [{"content": {"role": "model", "parts": [{"text": "hi"}]}, "finishReason": "STOP"}]}})
    )

    auth = GeminiCliAuth(http, clock=FakeClock())
    client = GeminiCliClient(http=http, auth=auth)
    resource = make_resource()
    resp = await client.post(resource, "https://cloudcode-pa.googleapis.com", {"model": "x", "project": "p", "request": {}})

    assert resp.status_code == 200
    assert resp.json()["response"]["candidates"][0]["content"]["parts"][0]["text"] == "hi"
    # 4 post calls total: token refresh, 401 call, token refresh, retry call
    assert len(http.post_calls) == 4
    # The retry used the refreshed bearer token.
    headers = http.post_calls[3]["headers"]
    assert headers["Authorization"] == "Bearer token-B"


async def test_401_retry_only_once():
    http = FakeHttp()
    http.responses.append(http.token_ok("token-A", 3600))
    http.responses.append(http.err(401, {"error": {"message": "no"}}))
    http.responses.append(http.token_ok("token-B", 3600))
    http.responses.append(http.err(401, {"error": {"message": "still no"}}))

    from providers.gemini_cli.client import GeminiCliClient

    auth = GeminiCliAuth(http, clock=FakeClock())
    client = GeminiCliClient(http=http, auth=auth)
    with pytest.raises(Exception) as excinfo:
        await client.post(make_resource(), "https://cloudcode-pa.googleapis.com", {"model": "x", "project": "p", "request": {}})
    from core.errors import AuthenticationError

    assert isinstance(excinfo.value, AuthenticationError)


async def test_no_infinite_refresh():
    http = FakeHttp()
    resource = make_resource(refresh_token="")
    auth = GeminiCliAuth(http, clock=FakeClock())
    with pytest.raises(GeminiCliAuthError):
        await auth.get_access_token(resource)


async def test_refresh_failure_capped():
    http = FakeHttp()
    http.responses.append(http.err(400, {"error": "bad refresh"}))
    http.responses.append(http.err(400, {"error": "bad refresh"}))
    http.responses.append(http.err(400, {"error": "bad refresh"}))
    auth = GeminiCliAuth(http, clock=FakeClock())
    resource = make_resource()
    with pytest.raises(GeminiCliAuthError):
        await auth.get_access_token(resource)


async def test_credentials_not_in_logs(caplog):
    import logging

    http = FakeHttp()
    http.responses.append(http.token_ok("super-secret-token", 3600))
    auth = GeminiCliAuth(http, clock=FakeClock())
    resource = make_resource()
    with caplog.at_level(logging.DEBUG):
        await auth.get_access_token(resource)
    assert "super-secret-token" not in caplog.text
    assert "refresh-token-1" not in caplog.text
    assert "client-secret-1" not in caplog.text


async def test_redacted_dict_masks_credentials():
    resource = make_resource(access_token="at-secret", refresh_token="rt-secret")
    redacted = resource.redacted_dict()
    assert "at-secret" not in str(redacted)
    assert "rt-secret" not in str(redacted)
    assert redacted["access_token"] == "***"
    assert redacted["refresh_token"] == "***"


async def test_refresh_transport_error_maps_to_network():
    import httpx

    http = FakeHttp()
    http.raise_exc = httpx.ConnectError("boom")
    auth = GeminiCliAuth(http, clock=FakeClock())
    with pytest.raises(GeminiCliNetworkError):
        await auth.get_access_token(make_resource())
