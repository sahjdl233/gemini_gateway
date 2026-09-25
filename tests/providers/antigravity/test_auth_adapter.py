"""AntigravityAuthAdapter migration tests (TASK-AUTH-006).

Behaviour-preservation + new-refresh-capability suite.  The adapter wraps
the new AntigravityAuth OAuth implementation (the first refresh
implementation for this provider); every test pins either a pre-migration
behaviour (static-token compat, no 401 retry, resource-scoped cache,
single-flight) or the newly introduced, test-locked refresh semantics.
"""

from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

from core.auth_adapter import (
    CredentialRefreshFailure,
    CredentialUnavailableError,
    InvalidCredentialError,
    ProviderAuthAdapter,
    RuntimeCredentials,
)
from core.credential import Credential, CredentialStore, CredentialType
from core.errors import (
    AuthenticationError,
    NetworkError,
    RateLimitError,
    TimeoutError,
    is_retryable,
)
from providers.antigravity.auth import (
    MAX_REFRESH_ATTEMPTS,
    PRE_REFRESH_SECONDS,
    AntigravityAuth,
)
from providers.antigravity.auth_adapter import AntigravityAuthAdapter
from providers.antigravity.resource import AntigravityResource


class FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body or {}

    def json(self):
        return self._json


class FakeHttp:
    """Token-endpoint http double (post(url, *, headers, data))."""

    def __init__(self):
        self.post_calls: List[dict] = []
        self.responses: List[FakeResponse] = []
        self.raise_exc: Any = None

    async def post(self, url, *, headers=None, data=None):
        self.post_calls.append(
            {"url": url, "headers": headers, "data": data}
        )
        if self.raise_exc is not None:
            exc = self.raise_exc
            self.raise_exc = None
            raise exc
        return self.responses.pop(0)

    @staticmethod
    def token_ok(token="token-A", expires_in=3600, **extra):
        body = {"access_token": token, "expires_in": expires_in}
        body.update(extra)
        return FakeResponse(200, body)


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.value = start

    def time(self) -> float:
        return self.value


def oauth_credential(**overrides) -> Credential:
    payload = {
        "refresh_token": "cred-refresh-token",
        "client_id": "cred-client-id",
        "client_secret": "cred-client-secret",
    }
    payload.update(overrides)
    return Credential(
        id="antigravity-oauth-01", type=CredentialType.OAUTH, payload=payload
    )


def static_credential() -> Credential:
    """AUTH-002 compat credential: static access_token only."""
    return Credential(
        id="antigravity-oauth-01",
        type=CredentialType.OAUTH,
        payload={"access_token": "static-access-token"},
    )


def make_adapter(store=None, *, clock=None, http=None) -> AntigravityAuthAdapter:
    return AntigravityAuthAdapter(
        http=http if http is not None else FakeHttp(),
        credential_store=store,
        clock=clock,
    )


def credentialless_resource() -> AntigravityResource:
    return AntigravityResource(
        id="a", credential_id="antigravity-oauth-01", project_id="proj"
    )


def static_resource() -> AntigravityResource:
    """Pre-AUTH-006 resource: static token only, no refresh material."""
    return AntigravityResource(id="a", access_token="legacy-token", project_id="p")


def legacy_refresh_resource() -> AntigravityResource:
    """Legacy fields carrying full refresh material (no credential_id)."""
    return AntigravityResource(
        id="a",
        access_token="legacy-token",
        refresh_token="legacy-refresh-token",
        client_id="legacy-client-id",
        client_secret="legacy-client-secret",
        project_id="p",
    )


# -- validate ----------------------------------------------------------------------


async def test_validate_accepts_complete_credential():
    adapter = make_adapter()
    await adapter.validate(oauth_credential())  # no raise


@pytest.mark.parametrize("missing", ["refresh_token", "client_id", "client_secret"])
async def test_validate_rejects_missing_field(missing):
    adapter = make_adapter()
    with pytest.raises(InvalidCredentialError) as exc_info:
        await adapter.validate(oauth_credential(**{missing: ""}))
    assert missing in str(exc_info.value)
    assert "cred-refresh-token" not in str(exc_info.value)
    assert "cred-client-secret" not in str(exc_info.value)


async def test_validate_performs_no_network_request():
    http = FakeHttp()
    adapter = make_adapter(http=http)
    await adapter.validate(oauth_credential())
    assert http.post_calls == []


async def test_validate_rejects_unsupported_type():
    adapter = make_adapter()
    api_key_cred = Credential(
        id="k1", type=CredentialType.API_KEY, payload={"api_key": "k"}
    )
    with pytest.raises(CredentialUnavailableError):
        await adapter.validate(api_key_cred)


# -- 29/31 credential path + token lifecycle -----------------------------------------


async def test_credential_path_refreshes_and_returns_runtime_credentials():
    http = FakeHttp()
    http.responses.append(http.token_ok("cred-access-token", 3600))
    adapter = make_adapter(
        _store_with(oauth_credential()), clock=FakeClock(), http=http
    )

    runtime = await adapter.get_runtime_credentials(
        oauth_credential(), credentialless_resource()
    )

    assert isinstance(runtime, RuntimeCredentials)
    assert runtime.headers == {"Authorization": "Bearer cred-access-token"}
    assert runtime.metadata == {}
    assert runtime.expires_at == 1000.0 + 3600.0
    (refresh_call,) = http.post_calls
    assert refresh_call["url"] == "https://oauth2.googleapis.com/token"
    assert refresh_call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert refresh_call["data"] == {
        "client_id": "cred-client-id",
        "client_secret": "cred-client-secret",
        "refresh_token": "cred-refresh-token",
        "grant_type": "refresh_token",
    }


def _store_with(credential: Credential) -> CredentialStore:
    store = CredentialStore()
    store.add(credential)
    return store


async def test_cached_token_served_within_validity():
    http = FakeHttp()
    http.responses.append(http.token_ok("token-A"))
    adapter = make_adapter(clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    first = await adapter.get_runtime_credentials(cred, resource)
    second = await adapter.get_runtime_credentials(cred, resource)

    assert first.headers == second.headers
    assert len(http.post_calls) == 1


async def test_pre_refresh_window_is_180_seconds_new_behaviour():
    """AUTH-006 NEW behaviour, test-locked: 180s pre-refresh window."""
    assert PRE_REFRESH_SECONDS == 180
    clock = FakeClock()
    http = FakeHttp()
    http.responses.append(http.token_ok("token-A"))
    http.responses.append(http.token_ok("token-B"))
    adapter = make_adapter(clock=clock, http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    await adapter.get_runtime_credentials(cred, resource)
    clock.value = 4600 - PRE_REFRESH_SECONDS
    refreshed = await adapter.get_runtime_credentials(cred, resource)

    assert refreshed.headers == {"Authorization": "Bearer token-B"}
    assert len(http.post_calls) == 2


# -- 29 legacy compatibility -----------------------------------------------------------


async def test_static_token_resource_keeps_pre_auth006_behavior():
    """Legacy static token via the live resolver path: used as-is, no
    expiry, no network calls."""
    http = FakeHttp()
    adapter = make_adapter(clock=FakeClock(), http=http)

    runtime = await adapter.auth.get_access_token(static_resource())
    second = await adapter.auth.get_access_token(static_resource())

    assert runtime == "legacy-token"
    assert second == runtime
    assert adapter.auth.expires_at is None  # no expiry semantics
    assert http.post_calls == []  # no token endpoint call at all


async def test_explicit_static_credential_seeds_runtime_token():
    """Contract path with an AUTH-002 static-token credential: the
    payload access_token seeds the runtime (compat behaviour)."""
    http = FakeHttp()
    adapter = make_adapter(clock=FakeClock(), http=http)

    runtime = await adapter.get_runtime_credentials(
        static_credential(), static_resource()
    )

    assert runtime.headers == {"Authorization": "Bearer static-access-token"}
    assert runtime.expires_at is None
    assert http.post_calls == []


async def test_legacy_refresh_material_works_without_credential():
    http = FakeHttp()
    http.responses.append(http.token_ok("legacy-access-token", 3600))
    adapter = make_adapter(clock=FakeClock(), http=http)

    runtime = await adapter.auth.get_access_token(legacy_refresh_resource())

    assert runtime == "legacy-access-token"
    (refresh_call,) = http.post_calls
    assert refresh_call["data"]["refresh_token"] == "legacy-refresh-token"
    assert refresh_call["data"]["client_id"] == "legacy-client-id"


# -- 30 precedence ------------------------------------------------------------------------


async def test_credential_path_wins_over_legacy_fields():
    store = _store_with(oauth_credential())
    http = FakeHttp()
    http.responses.append(http.token_ok("cred-access-token"))
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    # resource carries BOTH credential_id and legacy refresh material
    resource = legacy_refresh_resource()
    resource.credential_id = "antigravity-oauth-01"

    await adapter.get_runtime_credentials(oauth_credential(), resource)

    (refresh_call,) = http.post_calls
    assert refresh_call["data"]["refresh_token"] == "cred-refresh-token"
    assert refresh_call["data"]["client_id"] == "cred-client-id"


# -- 13 refresh force + failure mapping ----------------------------------------------------


async def test_refresh_forces_new_token():
    http = FakeHttp()
    http.responses.append(http.token_ok("token-A"))
    http.responses.append(http.token_ok("token-B"))
    adapter = make_adapter(clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    await adapter.get_runtime_credentials(cred, resource)
    refreshed = await adapter.refresh(cred, resource)

    assert refreshed.headers == {"Authorization": "Bearer token-B"}
    assert len(http.post_calls) == 2


async def test_refresh_without_material_maps_to_credential_refresh_failure():
    http = FakeHttp()
    adapter = make_adapter(clock=FakeClock(), http=http)

    with pytest.raises(CredentialRefreshFailure):
        await adapter.refresh(static_credential(), static_resource())
    assert http.post_calls == []


async def test_refresh_auth_failure_maps_to_credential_refresh_failure():
    http = FakeHttp()
    http.responses.append(FakeResponse(400, {"error": "invalid_grant"}))
    http.responses.append(FakeResponse(400, {"error": "invalid_grant"}))
    adapter = make_adapter(clock=FakeClock(), http=http)

    with pytest.raises(CredentialRefreshFailure):
        await adapter.refresh(oauth_credential(), credentialless_resource())
    with pytest.raises(CredentialRefreshFailure) as exc_info:
        await adapter.refresh(oauth_credential(), credentialless_resource())
    # no secrets in the adapter error message
    assert "cred-refresh-token" not in str(exc_info.value)
    assert "cred-client-secret" not in str(exc_info.value)


async def test_refresh_cap_prevents_infinite_refresh():
    http = FakeHttp()
    for _ in range(MAX_REFRESH_ATTEMPTS):
        http.responses.append(FakeResponse(400, {"error": "invalid_grant"}))
    adapter = make_adapter(clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    for _ in range(MAX_REFRESH_ATTEMPTS):
        with pytest.raises(CredentialRefreshFailure):
            await adapter.refresh(cred, resource)

    with pytest.raises(CredentialRefreshFailure):
        await adapter.refresh(cred, resource)
    assert len(http.post_calls) == MAX_REFRESH_ATTEMPTS


# -- 21/22/36 429 / network / timeout semantics ----------------------------------------------


async def test_token_endpoint_429_keeps_rate_limit_semantics():
    """429 at the token endpoint is capacity, never a credential failure."""
    http = FakeHttp()
    http.responses.append(FakeResponse(429, {"error": "throttled"}))
    adapter = make_adapter(clock=FakeClock(), http=http)

    with pytest.raises(RateLimitError) as exc_info:
        await adapter.refresh(oauth_credential(), credentialless_resource())

    assert not isinstance(exc_info.value, CredentialRefreshFailure)
    assert not isinstance(exc_info.value, InvalidCredentialError)
    assert is_retryable(exc_info.value) is True


async def test_network_failure_keeps_retryable_network_semantics():
    http = FakeHttp()
    http.raise_exc = RuntimeError("connection refused")
    adapter = make_adapter(clock=FakeClock(), http=http)

    with pytest.raises(NetworkError) as exc_info:
        await adapter.refresh(oauth_credential(), credentialless_resource())

    assert is_retryable(exc_info.value) is True
    assert not isinstance(exc_info.value, CredentialRefreshFailure)


async def test_timeout_keeps_retryable_timeout_semantics():
    http = FakeHttp()
    http.raise_exc = asyncio.TimeoutError()
    adapter = make_adapter(clock=FakeClock(), http=http)

    with pytest.raises(TimeoutError) as exc_info:
        await adapter.refresh(oauth_credential(), credentialless_resource())

    assert is_retryable(exc_info.value) is True


async def test_malformed_token_response_maps_to_credential_refresh_failure():
    """HTTP 200 without access_token: no KeyError leakage."""
    http = FakeHttp()
    http.responses.append(FakeResponse(200, {"error": "no token here"}))
    adapter = make_adapter(clock=FakeClock(), http=http)

    with pytest.raises(CredentialRefreshFailure) as exc_info:
        await adapter.refresh(oauth_credential(), credentialless_resource())

    assert isinstance(exc_info.value.__cause__, AuthenticationError)
    assert "cred-refresh-token" not in str(exc_info.value)


# -- 32 refresh token rotation -----------------------------------------------------------------


async def test_refresh_token_rotation_is_runtime_only():
    """A rotated refresh_token from the response is used for subsequent
    refreshes but NEVER written back into the Credential."""
    http = FakeHttp()
    http.responses.append(
        http.token_ok("token-A", refresh_token="rotated-refresh-token")
    )
    http.responses.append(http.token_ok("token-B"))
    store = _store_with(oauth_credential())
    credential = store.get("antigravity-oauth-01")
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    await adapter.get_runtime_credentials(cred, resource)
    await adapter.refresh(cred, resource)

    first_call, second_call = http.post_calls
    assert first_call["data"]["refresh_token"] == "cred-refresh-token"
    assert second_call["data"]["refresh_token"] == "rotated-refresh-token"
    # durable credential untouched (no accidental persistence)
    assert credential.payload["refresh_token"] == "cred-refresh-token"
    assert "rotated-refresh-token" not in str(credential.payload)


async def test_no_rotation_in_response_keeps_original_token():
    http = FakeHttp()
    http.responses.append(http.token_ok("token-A"))  # no refresh_token field
    http.responses.append(http.token_ok("token-B"))
    adapter = make_adapter(clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    await adapter.get_runtime_credentials(cred, resource)
    await adapter.refresh(cred, resource)

    first_call, second_call = http.post_calls
    assert first_call["data"]["refresh_token"] == "cred-refresh-token"
    assert second_call["data"]["refresh_token"] == "cred-refresh-token"


# -- 16/19/37 invalidate + 401 --------------------------------------------------------------------


async def test_invalidate_drops_runtime_cache_not_credential():
    http = FakeHttp()
    http.responses.append(http.token_ok("token-A"))
    http.responses.append(http.token_ok("token-B"))
    store = _store_with(oauth_credential())
    credential = store.get("antigravity-oauth-01")
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    await adapter.get_runtime_credentials(cred, resource)
    await adapter.invalidate(cred, resource)
    refreshed = await adapter.get_runtime_credentials(cred, resource)

    assert refreshed.headers == {"Authorization": "Bearer token-B"}
    assert len(http.post_calls) == 2
    assert credential.payload["refresh_token"] == "cred-refresh-token"


async def test_invalidate_on_static_resource_reseeds_without_network():
    http = FakeHttp()
    adapter = make_adapter(clock=FakeClock(), http=http)

    await adapter.auth.get_access_token(static_resource())
    adapter.auth.invalidate()
    token = await adapter.auth.get_access_token(static_resource())

    assert token == "legacy-token"
    assert http.post_calls == []


async def test_api_401_surfaces_as_authentication_error_no_retry():
    """401 BEFORE: surfaces as core AuthenticationError, no retry loop.
    401 AFTER: unchanged (AUTH-006 adds refresh but not a retry loop)."""
    from providers.antigravity.client import AntigravityClient

    class Api401Backend:
        def __init__(self):
            self.api_calls = 0

        async def execute(self, method, url, **kwargs):
            self.api_calls += 1
            return FakeResponse(401, {"error": "unauthorized"})

        async def close(self):
            pass

    backend = Api401Backend()
    http = FakeHttp()
    http.responses.append(http.token_ok("token-A"))
    adapter = make_adapter(
        _store_with(oauth_credential()), clock=FakeClock(), http=http
    )
    client = AntigravityClient(
        backend=backend,
        token_resolver=lambda resource: adapter.auth.get_access_token(resource),
    )

    with pytest.raises(AuthenticationError):
        await client.fetch_available_models(credentialless_resource())

    # exactly one API attempt (no retry loop); token fetched exactly once
    assert backend.api_calls == 1
    assert len(http.post_calls) == 1


# -- 18/34 concurrency ------------------------------------------------------------------------------


async def test_concurrent_get_runtime_credentials_single_flight():
    """10 concurrent acquisitions, no cached token -> exactly one refresh."""
    http = FakeHttp()
    http.responses.append(http.token_ok("token-A"))
    adapter = make_adapter(clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    runtimes = await asyncio.gather(
        *(adapter.get_runtime_credentials(cred, resource) for _ in range(10))
    )

    assert all(
        r.headers == {"Authorization": "Bearer token-A"} for r in runtimes
    )
    assert len(http.post_calls) == 1


# -- 35 shared credential: resource-scoped runtime cache ----------------------------------------------


async def test_shared_credential_resource_scoped_runtime_cache():
    http = FakeHttp()
    http.responses.append(http.token_ok("token-A"))
    http.responses.append(http.token_ok("token-B"))
    http.responses.append(http.token_ok("token-C"))
    store = _store_with(oauth_credential())
    adapter_a = make_adapter(store, clock=FakeClock(), http=http)
    adapter_b = make_adapter(store, clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource_a = AntigravityResource(id="A", credential_id="antigravity-oauth-01")
    resource_b = AntigravityResource(id="B", credential_id="antigravity-oauth-01")

    runtime_a1 = await adapter_a.get_runtime_credentials(cred, resource_a)
    runtime_b1 = await adapter_b.get_runtime_credentials(cred, resource_b)
    assert runtime_a1.headers == {"Authorization": "Bearer token-A"}
    assert runtime_b1.headers == {"Authorization": "Bearer token-B"}
    assert len(http.post_calls) == 2  # separate caches -> separate refreshes

    await adapter_a.invalidate(cred, resource_a)
    runtime_b2 = await adapter_b.get_runtime_credentials(cred, resource_b)
    assert runtime_b2.headers == {"Authorization": "Bearer token-B"}
    assert len(http.post_calls) == 2

    runtime_a2 = await adapter_a.get_runtime_credentials(cred, resource_a)
    assert runtime_a2.headers == {"Authorization": "Bearer token-C"}
    assert len(http.post_calls) == 3


# -- 37 security -----------------------------------------------------------------------------------------


async def test_runtime_token_never_written_back_into_credential_or_resource():
    http = FakeHttp()
    http.responses.append(http.token_ok("token-A"))
    store = _store_with(oauth_credential())
    credential = store.get("antigravity-oauth-01")
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    resource = credentialless_resource()

    await adapter.get_runtime_credentials(oauth_credential(), resource)

    assert set(credential.payload) == {"refresh_token", "client_id", "client_secret"}
    assert "token-A" not in str(credential.payload)
    # the resource's (empty) access_token field is not mutated either
    assert resource.access_token is None


def test_runtime_credentials_repr_masks_bearer_token():
    runtime = RuntimeCredentials(
        headers={"Authorization": "Bearer super-secret-token"},
        metadata={},
        expires_at=4600.0,
    )
    rendered = repr(runtime) + str(runtime) + str(runtime.redacted_dict())
    assert "super-secret-token" not in rendered
    assert "Authorization" in rendered


# -- provider wiring ---------------------------------------------------------------------------------------


def test_provider_builds_one_adapter_per_resource():
    from providers.antigravity.provider import AntigravityProvider

    provider = AntigravityProvider()

    async def check():
        a = await provider._adapter_for(AntigravityResource(id="A"))
        b = await provider._adapter_for(AntigravityResource(id="B"))
        a2 = await provider._adapter_for(AntigravityResource(id="A"))
        assert a is not b
        assert a is a2
        assert isinstance(a, ProviderAuthAdapter)

    asyncio.run(check())


def test_adapter_auth_uses_clock_and_exposes_impl():
    adapter = make_adapter(clock=FakeClock())
    assert isinstance(adapter.auth, AntigravityAuth)
