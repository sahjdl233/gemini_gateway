"""FirebaseAuthAdapter migration tests (TASK-AUTH-005).

Behaviour-preservation suite: the adapter wraps the existing FirebaseAuth
App Check implementation; every test here pins one pre-migration
behaviour (300s pre-refresh window, JWT cache, single-flight lock,
invalidate, 401 semantics, resource-scoped runtime cache) on the AUTH-003
contract surface.  Firebase is deliberately NOT treated as OAuth.
"""

from __future__ import annotations

import asyncio

import pytest

from core.auth_adapter import (
    CredentialRefreshFailure,
    CredentialUnavailableError,
    InvalidCredentialError,
    ProviderAuthAdapter,
    RuntimeCredentials,
)
from core.credential import Credential, CredentialStore, CredentialType
from core.errors import RateLimitError
from providers.firebase.auth import FirebaseAuth
from providers.firebase.auth_adapter import FirebaseAuthAdapter
from providers.firebase.client import FirebaseClient
from providers.firebase.errors import (
    FirebaseNetworkError,
    FirebaseAuthError,
    FirebaseTimeoutError,
)

from tests.providers._firebase_fakes import FakeHttp, FakeResponse, make_resource


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.value = start

    def time(self) -> float:
        return self.value


def api_key_credential(**overrides) -> Credential:
    payload = {
        "api_key": "cred-api-key",
        "app_id": "cred-app-id",
        "debug_token": "cred-debug-token",
    }
    payload.update(overrides)
    return Credential(id="firebase-cred-01", type=CredentialType.API_KEY, payload=payload)


def make_adapter(store=None, *, clock=None, http=None) -> FirebaseAuthAdapter:
    return FirebaseAuthAdapter(
        http=http if http is not None else FakeHttp(),
        credential_store=store,
        clock=clock,
    )


def credentialless_resource() -> object:
    """New-path resource: credential_id + project identity only."""
    return make_resource(
        credential_id="firebase-cred-01",
        api_key="",
        app_id="",
        debug_token="",
    )


def legacy_resource() -> object:
    """Old-path resource: no credential_id, legacy fields present."""
    return make_resource(credential_id=None)


def queue_exchange(http: FakeHttp, tokens: list) -> None:
    for token in tokens:
        http.responses.append(http.exchange_ok(token, "3600s"))


# -- 25.1 credential path -------------------------------------------------------


async def test_credential_path_yields_runtime_credentials():
    store = CredentialStore()
    store.add(api_key_credential())
    http = FakeHttp()
    queue_exchange(http, ["jwt-token-1"])
    adapter = make_adapter(store, clock=FakeClock(), http=http)

    runtime = await adapter.get_runtime_credentials(
        api_key_credential(), credentialless_resource()
    )

    assert isinstance(runtime, RuntimeCredentials)
    assert runtime.headers["X-Firebase-AppCheck"] == "jwt-token-1"
    assert runtime.headers["x-goog-api-key"] == "cred-api-key"
    assert runtime.headers["X-Firebase-Appid"] == "cred-app-id"
    assert runtime.metadata == {"project_id": "test-project"}
    assert runtime.expires_at == 1000.0 + 3600.0


async def test_exchange_request_uses_credential_material():
    """The exchange POST carries debug_token/api_key/app_id from the
    Credential payload and project_id from the Resource."""
    store = CredentialStore()
    store.add(api_key_credential())
    http = FakeHttp()
    queue_exchange(http, ["jwt-token-1"])
    adapter = make_adapter(store, clock=FakeClock(), http=http)

    await adapter.get_runtime_credentials(
        api_key_credential(), credentialless_resource()
    )

    (exchange_call,) = http.post_calls
    assert "firebaseappcheck.googleapis.com/v1/projects/test-project" in exchange_call["url"]
    assert "apps/cred-app-id:exchangeDebugToken" in exchange_call["url"]
    assert "key=cred-api-key" in exchange_call["url"]
    assert exchange_call["json"] == {"debug_token": "cred-debug-token", "limited_use": False}


# -- 25.2 legacy path -------------------------------------------------------------


async def test_legacy_resource_without_credential_still_works():
    """Old config (no credential_id, legacy fields) keeps working through
    the live request path."""
    http = FakeHttp()
    queue_exchange(http, ["jwt-legacy"])
    http.responses.append(FakeResponse(200, json_body={"candidates": []}))
    adapter = make_adapter(clock=FakeClock(), http=http)
    client = FirebaseClient(
        http=http, auth=adapter.auth, material_resolver=adapter.material_for
    )

    await client.complete(legacy_resource(), "gemini-3.8-flash", {"contents": []})

    exchange_call, api_call = http.post_calls
    assert "key=AIzaSyTESTAPIKEY" in exchange_call["url"]
    assert exchange_call["json"] == {"debug_token": "debug-token-0000", "limited_use": False}
    assert api_call["headers"]["X-Firebase-AppCheck"] == "jwt-legacy"


# -- 25.3 precedence ----------------------------------------------------------------


async def test_credential_path_wins_over_legacy_fields():
    """Conflict precedence: credential_id (resolvable, api_key) > legacy,
    per field; Resource.project_id always wins over payload project_id."""
    store = CredentialStore()
    store.add(api_key_credential())
    http = FakeHttp()
    queue_exchange(http, ["jwt-cred"])
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    # resource carries BOTH credential_id and legacy fields
    resource = make_resource(credential_id="firebase-cred-01")

    await adapter.get_runtime_credentials(api_key_credential(), resource)

    (exchange_call,) = http.post_calls
    assert "projects/test-project/" in exchange_call["url"]  # Resource wins
    assert "key=cred-api-key" in exchange_call["url"]  # Credential wins
    assert exchange_call["json"] == {"debug_token": "cred-debug-token", "limited_use": False}


# -- 9 validate -----------------------------------------------------------------------


async def test_validate_accepts_complete_credential():
    adapter = make_adapter()
    await adapter.validate(api_key_credential())  # no raise


@pytest.mark.parametrize("missing", ["api_key", "app_id", "debug_token"])
async def test_validate_rejects_missing_field(missing):
    adapter = make_adapter()
    payload = {
        "api_key": "cred-api-key",
        "app_id": "cred-app-id",
        "debug_token": "cred-debug-token",
    }
    payload[missing] = ""
    with pytest.raises(InvalidCredentialError) as exc_info:
        await adapter.validate(
            Credential(id="firebase-cred-01", type=CredentialType.API_KEY, payload=payload)
        )
    assert missing in str(exc_info.value)
    # no secrets in the message
    assert "cred-api-key" not in str(exc_info.value)
    assert "cred-debug-token" not in str(exc_info.value)


async def test_validate_performs_no_network_request():
    http = FakeHttp()
    adapter = make_adapter(http=http)
    await adapter.validate(api_key_credential())
    assert http.post_calls == []


async def test_validate_rejects_unsupported_type():
    adapter = make_adapter()
    oauth_cred = Credential(
        id="o1",
        type=CredentialType.OAUTH,
        payload={"refresh_token": "rt", "client_id": "cid", "client_secret": "cs"},
    )
    with pytest.raises(CredentialUnavailableError):
        await adapter.validate(oauth_cred)


# -- 26 JWT cache (FirebaseAuth real expiry semantics: 300s pre-refresh) ---------------


async def test_cached_jwt_served_within_validity():
    assert FirebaseAuth.PRE_REFRESH_SECONDS == 300
    http = FakeHttp()
    queue_exchange(http, ["jwt-1"])
    adapter = make_adapter(clock=FakeClock(), http=http)
    cred = api_key_credential()
    resource = credentialless_resource()

    first = await adapter.get_runtime_credentials(cred, resource)
    second = await adapter.get_runtime_credentials(cred, resource)

    assert first.headers == second.headers
    assert len(http.post_calls) == 1  # one exchange only


async def test_pre_refresh_window_preserved():
    clock = FakeClock()
    http = FakeHttp()
    queue_exchange(http, ["jwt-1", "jwt-2"])
    adapter = make_adapter(clock=clock, http=http)
    cred = api_key_credential()
    resource = credentialless_resource()

    await adapter.get_runtime_credentials(cred, resource)
    # 3600s TTL from t=1000 -> expiry 4600; jump to the 300s threshold
    clock.value = 4600 - FirebaseAuth.PRE_REFRESH_SECONDS
    refreshed = await adapter.get_runtime_credentials(cred, resource)

    assert refreshed.headers["X-Firebase-AppCheck"] == "jwt-2"
    assert len(http.post_calls) == 2


# -- 27 refresh --------------------------------------------------------------------------


async def test_refresh_forces_new_exchange():
    http = FakeHttp()
    queue_exchange(http, ["jwt-1", "jwt-2"])
    adapter = make_adapter(clock=FakeClock(), http=http)
    cred = api_key_credential()
    resource = credentialless_resource()

    await adapter.get_runtime_credentials(cred, resource)
    refreshed = await adapter.refresh(cred, resource)

    assert refreshed.headers["X-Firebase-AppCheck"] == "jwt-2"
    assert len(http.post_calls) == 2


async def test_refresh_and_get_leave_credential_payload_unchanged():
    """The JWT never leaks back into durable Credential material."""
    http = FakeHttp()
    queue_exchange(http, ["jwt-1", "jwt-2"])
    store = CredentialStore()
    credential = store.add(api_key_credential())
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    cred = api_key_credential()
    resource = credentialless_resource()

    await adapter.get_runtime_credentials(cred, resource)
    await adapter.refresh(cred, resource)

    assert set(credential.payload) == {"api_key", "app_id", "debug_token"}
    assert "jwt-1" not in str(credential.payload)
    assert "jwt-2" not in str(credential.payload)


# -- 28 invalidate --------------------------------------------------------------------------


async def test_invalidate_drops_jwt_cache_not_credential():
    http = FakeHttp()
    queue_exchange(http, ["jwt-1", "jwt-2"])
    store = CredentialStore()
    credential = store.add(api_key_credential())
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    cred = api_key_credential()
    resource = credentialless_resource()

    await adapter.get_runtime_credentials(cred, resource)
    await adapter.invalidate(cred, resource)
    refreshed = await adapter.get_runtime_credentials(cred, resource)

    assert refreshed.headers["X-Firebase-AppCheck"] == "jwt-2"
    assert len(http.post_calls) == 2
    # durable material untouched by invalidate
    assert credential.payload["debug_token"] == "cred-debug-token"
    assert credential.payload["api_key"] == "cred-api-key"


# -- 29 concurrency ---------------------------------------------------------------------------


async def test_concurrent_get_runtime_credentials_single_flight():
    """N concurrent acquisitions with no cached JWT -> exactly one exchange."""
    http = FakeHttp()
    queue_exchange(http, ["jwt-1"])
    adapter = make_adapter(clock=FakeClock(), http=http)
    cred = api_key_credential()
    resource = credentialless_resource()

    runtimes = await asyncio.gather(
        *(adapter.get_runtime_credentials(cred, resource) for _ in range(10))
    )

    assert all(
        r.headers["X-Firebase-AppCheck"] == "jwt-1" for r in runtimes
    )
    assert len(http.post_calls) == 1  # single-flight exchange


# -- 30 shared credential: resource-scoped JWT cache -------------------------------------------


async def test_shared_credential_resource_scoped_jwt_cache():
    """Two resources sharing one credential_id each get their own adapter,
    JWT cache and lock: credential sharing != runtime cache sharing."""
    http = FakeHttp()
    queue_exchange(http, ["jwt-A", "jwt-B", "jwt-C"])
    store = CredentialStore()
    store.add(api_key_credential())
    adapter_a = make_adapter(store, clock=FakeClock(), http=http)
    adapter_b = make_adapter(store, clock=FakeClock(), http=http)
    cred = api_key_credential()
    resource_a = make_resource(id="A", credential_id="firebase-cred-01")
    resource_b = make_resource(id="B", credential_id="firebase-cred-01")

    runtime_a1 = await adapter_a.get_runtime_credentials(cred, resource_a)
    runtime_b1 = await adapter_b.get_runtime_credentials(cred, resource_b)
    assert runtime_a1.headers["X-Firebase-AppCheck"] == "jwt-A"
    assert runtime_b1.headers["X-Firebase-AppCheck"] == "jwt-B"
    assert len(http.post_calls) == 2  # separate caches -> separate exchanges

    # invalidating A does not affect B's runtime cache
    await adapter_a.invalidate(cred, resource_a)
    runtime_b2 = await adapter_b.get_runtime_credentials(cred, resource_b)
    assert runtime_b2.headers["X-Firebase-AppCheck"] == "jwt-B"
    assert len(http.post_calls) == 2

    # A exchanges afresh after invalidation
    runtime_a2 = await adapter_a.get_runtime_credentials(cred, resource_a)
    assert runtime_a2.headers["X-Firebase-AppCheck"] == "jwt-C"
    assert len(http.post_calls) == 3


# -- 31 error mapping ---------------------------------------------------------------------------


async def test_exchange_auth_failure_maps_to_credential_refresh_failure():
    """Exchange non-200 (FirebaseAuthError, an AuthenticationError) surfaces
    as CredentialRefreshFailure on the contract surface."""
    http = FakeHttp()
    http.responses.append(FakeResponse(400, json_body={"error": {"message": "invalid debug token"}}))
    adapter = make_adapter(clock=FakeClock(), http=http)

    with pytest.raises(CredentialRefreshFailure) as exc_info:
        await adapter.refresh(api_key_credential(), credentialless_resource())

    assert "cred-debug-token" not in str(exc_info.value)
    assert "invalid debug token" not in str(exc_info.value)  # body not echoed
    assert isinstance(exc_info.value.__cause__, FirebaseAuthError)


async def test_exchange_network_failure_keeps_network_semantics():
    """Network failure is NOT a credential failure (stays retryable)."""
    http = FakeHttp()
    http.raise_exc = RuntimeError("connection refused")
    adapter = make_adapter(clock=FakeClock(), http=http)

    with pytest.raises(FirebaseNetworkError):
        await adapter.refresh(api_key_credential(), credentialless_resource())

    from core.errors import is_retryable

    http2 = FakeHttp()
    http2.raise_exc = RuntimeError("connection refused")
    adapter2 = make_adapter(clock=FakeClock(), http=http2)
    with pytest.raises(FirebaseNetworkError) as exc_info:
        await adapter2.refresh(api_key_credential(), credentialless_resource())
    assert is_retryable(exc_info.value) is True


async def test_exchange_timeout_keeps_timeout_semantics():
    http = FakeHttp()
    http.raise_exc = asyncio.TimeoutError()
    adapter = make_adapter(clock=FakeClock(), http=http)

    with pytest.raises(FirebaseTimeoutError):
        await adapter.refresh(api_key_credential(), credentialless_resource())


async def test_exchange_429_stays_rate_limit_not_credential_failure():
    """TASK-AUTH-016: an exchange-endpoint 429 is capacity, not a broken
    credential.

    FirebaseAuth classifies 429 as ``RateLimitError`` (carrying the parsed
    Retry-After), and the adapter contract surface lets it through
    untouched so the Scheduler can cooldown the resource.  It is never an
    ``InvalidCredentialError`` and never a ``CredentialRefreshFailure``.
    """
    http = FakeHttp()
    http.responses.append(
        FakeResponse(
            429,
            json_body={"error": {"message": "throttled"}},
            headers={"Retry-After": "12"},
        )
    )
    adapter = make_adapter(clock=FakeClock(), http=http)

    with pytest.raises(RateLimitError) as exc_info:
        await adapter.refresh(api_key_credential(), credentialless_resource())
    exc = exc_info.value
    assert not isinstance(exc, InvalidCredentialError)
    assert not isinstance(exc, CredentialRefreshFailure)
    assert exc.provider == "firebase"
    assert exc.retry_after == 12.0
    assert exc.default_status == 429


async def test_api_endpoint_429_keeps_rate_limit_semantics():
    """A 429 on the AI Logic API endpoint (client path) is rate limiting,
    never a credential failure — behaviour unchanged from pre-migration."""
    http = FakeHttp()
    queue_exchange(http, ["jwt-1"])
    from tests.providers._firebase_fakes import FakeResponse

    http.responses.append(
        FakeResponse(429, json_body={"error": {"message": "quota"}}, headers={"Retry-After": "2"})
    )
    # client path resolves via material_for -> strict credential binding:
    # the referenced credential must exist in the adapter's store
    store = CredentialStore()
    store.add(api_key_credential())
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    client = FirebaseClient(
        http=http, auth=adapter.auth, material_resolver=adapter.material_for
    )

    from core.errors import RateLimitError

    with pytest.raises(RateLimitError):
        await client.complete(
            credentialless_resource(), "gemini-3.8-flash", {"contents": []}
        )


# -- 17/32 401 behaviour + security ---------------------------------------------------------------


async def test_401_refresh_retry_once_preserved():
    """API endpoint 401 -> invalidate -> forced exchange -> retry once."""
    from tests.providers._firebase_fakes import FakeResponse

    http = FakeHttp()
    queue_exchange(http, ["jwt-stale"])
    http.responses.append(FakeResponse(401, json_body={"error": {"message": "unauthorized"}}))
    queue_exchange(http, ["jwt-fresh"])
    http.responses.append(FakeResponse(200, json_body={"candidates": []}))

    # client path resolves via material_for -> strict credential binding
    store = CredentialStore()
    store.add(api_key_credential())
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    client = FirebaseClient(
        http=http, auth=adapter.auth, material_resolver=adapter.material_for
    )

    resp = await client.complete(
        credentialless_resource(), "gemini-3.8-flash", {"contents": []}
    )

    assert resp.status_code == 200
    # exchange, 401 call, forced exchange, retried call
    assert len(http.post_calls) == 4
    assert http.post_calls[3]["headers"]["X-Firebase-AppCheck"] == "jwt-fresh"


def test_runtime_credentials_repr_masks_secrets():
    runtime = RuntimeCredentials(
        headers={
            "X-Firebase-AppCheck": "super-secret-jwt",
            "x-goog-api-key": "AIzaSySECRET",
        },
        metadata={"project_id": "proj-1"},
        expires_at=4600.0,
    )
    rendered = repr(runtime) + str(runtime) + str(runtime.redacted_dict())
    assert "super-secret-jwt" not in rendered
    assert "AIzaSySECRET" not in rendered
    assert "proj-1" in rendered


def test_provider_builds_one_adapter_per_resource():
    """Provider wiring: adapter cache is per resource.id (resource-scoped)."""
    from providers.firebase.provider import FirebaseProvider

    provider = FirebaseProvider()
    provider.set_http_client(FakeHttp())

    async def check():
        a = await provider._auth_adapter_for(make_resource(id="A"))
        b = await provider._auth_adapter_for(make_resource(id="B"))
        a2 = await provider._auth_adapter_for(make_resource(id="A"))
        assert a is not b
        assert a is a2
        assert isinstance(a, ProviderAuthAdapter)

    asyncio.run(check())
