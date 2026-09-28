"""GeminiCliAuthAdapter migration tests (TASK-AUTH-004).

Behaviour-preservation suite: the adapter wraps the existing
GeminiCliAuth OAuth implementation; every test here pins one pre-migration
behaviour (pre-refresh window, refresh cap, 401 retry, invalidate,
concurrency, resource-scoped runtime cache) on the contract surface.
"""

from __future__ import annotations

import asyncio

import pytest

from core.auth_adapter import (
    CredentialRefreshFailure,
    CredentialUnavailableError,
    InvalidCredentialError,
    RuntimeCredentials,
)
from core.auth_adapter import ProviderAuthAdapter
from core.credential import Credential, CredentialStore, CredentialType
from providers.gemini_cli.auth import MAX_REFRESH_ATTEMPTS, PRE_REFRESH_SECONDS
from providers.gemini_cli.auth_adapter import GeminiCliAuthAdapter
from providers.gemini_cli.client import GeminiCliClient

from tests.providers._gemini_cli_fakes import FakeHttp, make_backend, make_resource


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
    return Credential(id="google-oauth-01", type=CredentialType.OAUTH, payload=payload)


def make_adapter(store=None, *, clock=None, http=None) -> GeminiCliAuthAdapter:
    return GeminiCliAuthAdapter(
        http=http if http is not None else FakeHttp(),
        credential_store=store,
        clock=clock,
    )


def credentialless_resource() -> object:
    """New-path resource: credential_id only, legacy fields absent."""
    return make_resource(
        credential_id="google-oauth-01",
        access_token="",
        refresh_token="",
        client_id="",
        client_secret="",
    )


def legacy_resource() -> object:
    """Old-path resource: no credential_id, legacy OAuth fields present."""
    return make_resource(credential_id=None)


def queued_refresh_flow(http: FakeHttp, tokens: list) -> None:
    """Queue token refresh responses (+ optional API responses)."""
    for token in tokens:
        http.responses.append(http.token_ok(token, 3600))


# -- 19.1 credential path -------------------------------------------------------


async def test_credential_path_yields_runtime_credentials():
    store = CredentialStore()
    store.add(oauth_credential())
    http = FakeHttp()
    http.responses.append(http.token_ok("cred-access-token", 3600))
    adapter = make_adapter(store, clock=FakeClock(), http=http)

    runtime = await adapter.get_runtime_credentials(
        oauth_credential(), credentialless_resource()
    )

    assert isinstance(runtime, RuntimeCredentials)
    assert runtime.headers == {"Authorization": "Bearer cred-access-token"}
    assert runtime.expires_at == 1000.0 + 3600.0


async def test_runtime_credentials_come_from_credential_not_legacy_fields():
    """New-path resource with empty legacy fields still authenticates."""
    store = CredentialStore()
    store.add(oauth_credential())
    http = FakeHttp()
    http.responses.append(http.token_ok("cred-access-token", 3600))
    adapter = make_adapter(store, http=http)
    resource = legacy_resource()
    resource.credential_id = "google-oauth-01"
    resource.refresh_token = ""
    resource.client_id = ""
    resource.client_secret = ""

    await adapter.get_runtime_credentials(oauth_credential(), resource)

    (refresh_call,) = http.post_calls
    assert refresh_call["data"]["refresh_token"] == "cred-refresh-token"
    assert refresh_call["data"]["client_id"] == "cred-client-id"


async def test_adapter_requires_resource_context():
    adapter = make_adapter()
    with pytest.raises(CredentialUnavailableError):
        await adapter.get_runtime_credentials(oauth_credential(), None)


# -- 19.2 legacy compatibility (credential path stays canonical) -----------------


async def test_legacy_resource_without_credential_still_authenticates():
    """Old config (no credential_id, legacy fields) keeps working through
    the live request path: the adapter's resolver falls back to the
    legacy Resource fields."""
    http = FakeHttp()
    http.responses.append(http.token_ok("legacy-access-token", 3600))
    http.responses.append(http.ok(ok_generate("hi")))
    adapter = GeminiCliAuthAdapter(
        http=http, credential_store=None, clock=FakeClock()
    )
    client = GeminiCliClient(backend=make_backend(http), auth=adapter.auth)

    resp = await client.post(
        legacy_resource(), "https://cloudcode-pa.googleapis.com", {}
    )

    assert resp.status_code == 200
    refresh_call, api_call = http.post_calls
    assert refresh_call["data"]["refresh_token"] == "refresh-token-1"
    assert refresh_call["data"]["client_id"] == "client-id-1"
    assert api_call["headers"]["Authorization"] == "Bearer legacy-access-token"


async def test_credential_path_wins_over_legacy_fields():
    """Conflict precedence: credential_id (resolvable, oauth) > legacy."""
    store = CredentialStore()
    store.add(oauth_credential())
    http = FakeHttp()
    http.responses.append(http.token_ok("cred-access-token", 3600))
    adapter = make_adapter(store, http=http)
    # resource carries BOTH credential_id and legacy fields
    resource = make_resource(credential_id="google-oauth-01")

    await adapter.get_runtime_credentials(oauth_credential(), resource)

    (refresh_call,) = http.post_calls
    assert refresh_call["data"]["refresh_token"] == "cred-refresh-token"
    assert refresh_call["data"]["client_secret"] == "cred-client-secret"


# -- 8 validate --------------------------------------------------------------------


async def test_validate_accepts_complete_credential():
    adapter = make_adapter()
    await adapter.validate(oauth_credential())  # no raise


async def test_validate_rejects_missing_refresh_token():
    adapter = make_adapter()
    with pytest.raises(InvalidCredentialError) as exc_info:
        await adapter.validate(oauth_credential(refresh_token=""))
    assert "refresh_token" in str(exc_info.value)
    # no secrets in the message
    assert "cred-refresh-token" not in str(exc_info.value)


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


# -- 19.3/19.4 token cache + refresh ------------------------------------------------


async def test_cached_token_served_within_validity():
    http = FakeHttp()
    queued_refresh_flow(http, ["token-A"])
    adapter = make_adapter(clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    first = await adapter.get_runtime_credentials(cred, resource)
    second = await adapter.get_runtime_credentials(cred, resource)

    assert first.headers == second.headers
    assert len(http.post_calls) == 1  # one refresh only


async def test_pre_refresh_window_preserved():
    """180s pre-refresh window: at expiry-180 the token refreshes."""
    assert PRE_REFRESH_SECONDS == 180
    clock = FakeClock()
    http = FakeHttp()
    queued_refresh_flow(http, ["token-A", "token-B"])
    adapter = make_adapter(clock=clock, http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    await adapter.get_runtime_credentials(cred, resource)
    # 3600s validity from t=1000 -> expiry 4600; jump to the threshold
    clock.value = 4600 - PRE_REFRESH_SECONDS
    refreshed = await adapter.get_runtime_credentials(cred, resource)

    assert refreshed.headers == {"Authorization": "Bearer token-B"}
    assert len(http.post_calls) == 2


async def test_refresh_forces_new_token():
    http = FakeHttp()
    queued_refresh_flow(http, ["token-A", "token-B"])
    adapter = make_adapter(clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    await adapter.get_runtime_credentials(cred, resource)
    refreshed = await adapter.refresh(cred, resource)

    assert refreshed.headers == {"Authorization": "Bearer token-B"}
    assert len(http.post_calls) == 2


async def test_refresh_failure_maps_to_credential_refresh_failure():
    http = FakeHttp()
    http.responses.append(http.err(400, {"error": "bad refresh"}))
    http.responses.append(http.err(400, {"error": "bad refresh"}))
    adapter = make_adapter(clock=FakeClock(), http=http)

    with pytest.raises(CredentialRefreshFailure):
        await adapter.refresh(oauth_credential(), credentialless_resource())

    # no secrets in the adapter error message (body stays on the chained cause)
    with pytest.raises(CredentialRefreshFailure) as exc_info:
        await adapter.refresh(oauth_credential(), credentialless_resource())
    assert "cred-refresh-token" not in str(exc_info.value)
    assert "cred-client-secret" not in str(exc_info.value)


async def test_refresh_cap_preserved():
    """MAX_REFRESH_ATTEMPTS consecutive failures still trip the cap."""
    http = FakeHttp()
    for _ in range(MAX_REFRESH_ATTEMPTS):
        http.responses.append(http.err(400, {"error": "bad refresh"}))
    adapter = make_adapter(clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    for _ in range(MAX_REFRESH_ATTEMPTS):
        with pytest.raises(CredentialRefreshFailure):
            await adapter.refresh(cred, resource)

    # the implementation gave up: next call raises without another POST
    with pytest.raises(CredentialRefreshFailure):
        await adapter.refresh(cred, resource)
    assert len(http.post_calls) == MAX_REFRESH_ATTEMPTS


# -- 19.6 refresh loop prevention (401 retry through the live client path) --------


def ok_generate(text: str) -> dict:
    return {
        "response": {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}
            ]
        }
    }


async def test_401_force_refresh_retry_once_preserved():
    """401 -> invalidate -> force refresh -> retry once -> success."""
    http = FakeHttp()
    http.responses.append(http.token_ok("token-A", 3600))
    http.responses.append(http.err(401, {"error": {"message": "invalid token"}}))
    http.responses.append(http.token_ok("token-B", 3600))
    http.responses.append(http.ok(ok_generate("hi")))

    store = CredentialStore()
    store.add(oauth_credential())
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    client = GeminiCliClient(backend=make_backend(http), auth=adapter.auth)
    resource = credentialless_resource()

    resp = await client.post(resource, "https://cloudcode-pa.googleapis.com", {})

    assert resp.status_code == 200
    # refresh, 401 call, forced refresh, retried call
    assert len(http.post_calls) == 4
    assert http.post_calls[3]["headers"]["Authorization"] == "Bearer token-B"


async def test_401_then_refresh_failure_does_not_loop():
    http = FakeHttp()
    http.responses.append(http.token_ok("token-A", 3600))
    http.responses.append(http.err(401, {"error": {"message": "invalid token"}}))
    http.responses.append(http.err(400, {"error": "bad refresh"}))

    store = CredentialStore()
    store.add(oauth_credential())
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    client = GeminiCliClient(backend=make_backend(http), auth=adapter.auth)
    resource = credentialless_resource()

    from core.errors import AuthenticationError

    with pytest.raises(AuthenticationError):
        await client.post(resource, "https://cloudcode-pa.googleapis.com", {})
    # refresh, 401 call, failed refresh attempt — no further attempts
    assert len(http.post_calls) == 3


# -- 19.7 invalidate ---------------------------------------------------------------


async def test_invalidate_drops_cache_but_not_credential():
    http = FakeHttp()
    queued_refresh_flow(http, ["token-A", "token-B"])
    store = CredentialStore()
    credential = store.add(oauth_credential())
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    await adapter.get_runtime_credentials(cred, resource)
    await adapter.invalidate(cred, resource)
    refreshed = await adapter.get_runtime_credentials(cred, resource)

    assert refreshed.headers == {"Authorization": "Bearer token-B"}
    assert len(http.post_calls) == 2
    # durable material untouched by invalidate
    assert credential.payload["refresh_token"] == "cred-refresh-token"
    assert credential.payload["client_secret"] == "cred-client-secret"


async def test_runtime_token_never_written_back_into_credential():
    http = FakeHttp()
    queued_refresh_flow(http, ["token-A", "token-B"])
    store = CredentialStore()
    credential = store.add(oauth_credential())
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    await adapter.get_runtime_credentials(cred, resource)
    await adapter.refresh(cred, resource)

    assert set(credential.payload) == {"refresh_token", "client_id", "client_secret"}
    assert "token-A" not in str(credential.payload)
    assert "token-B" not in str(credential.payload)


# -- 19.8 concurrency ---------------------------------------------------------------


async def test_concurrent_get_runtime_credentials_single_flight():
    """N concurrent acquisitions with no valid token -> exactly one refresh."""
    http = FakeHttp()
    queued_refresh_flow(http, ["token-A"])
    adapter = make_adapter(clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    runtimes = await asyncio.gather(
        *(adapter.get_runtime_credentials(cred, resource) for _ in range(10))
    )

    assert all(r.headers == {"Authorization": "Bearer token-A"} for r in runtimes)
    assert len(http.post_calls) == 1  # single-flight refresh


# -- 19.9 shared credential: resource-scoped runtime cache ---------------------------


async def test_shared_credential_resource_scoped_runtime_cache():
    """Two resources sharing one credential_id each get their own adapter,
    token cache and lock: credential sharing != runtime cache sharing."""
    http = FakeHttp()
    queued_refresh_flow(http, ["token-A", "token-B", "token-C"])
    store = CredentialStore()
    store.add(oauth_credential())
    adapter_a = make_adapter(store, clock=FakeClock(), http=http)
    adapter_b = make_adapter(store, clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource_a = make_resource(id="A", credential_id="google-oauth-01")
    resource_b = make_resource(id="B", credential_id="google-oauth-01")

    runtime_a1 = await adapter_a.get_runtime_credentials(cred, resource_a)
    runtime_b1 = await adapter_b.get_runtime_credentials(cred, resource_b)
    assert runtime_a1.headers == {"Authorization": "Bearer token-A"}
    assert runtime_b1.headers == {"Authorization": "Bearer token-B"}
    assert len(http.post_calls) == 2  # separate caches -> separate refreshes

    # invalidating A does not affect B's runtime cache
    await adapter_a.invalidate(cred, resource_a)
    runtime_b2 = await adapter_b.get_runtime_credentials(cred, resource_b)
    assert runtime_b2.headers == {"Authorization": "Bearer token-B"}
    assert len(http.post_calls) == 2

    # A reacquires after invalidation
    runtime_a2 = await adapter_a.get_runtime_credentials(cred, resource_a)
    assert runtime_a2.headers == {"Authorization": "Bearer token-C"}
    assert len(http.post_calls) == 3


def test_provider_builds_one_adapter_per_resource():
    """Provider wiring: adapter cache is per resource.id (resource-scoped)."""
    from providers.gemini_cli.provider import GeminiCliProvider

    provider = GeminiCliProvider()
    provider.set_http_client(FakeHttp())

    async def check():
        a = await provider._auth_adapter_for(make_resource(id="A"))
        b = await provider._auth_adapter_for(make_resource(id="B"))
        a2 = await provider._auth_adapter_for(make_resource(id="A"))
        assert a is not b
        assert a is a2
        assert isinstance(a, ProviderAuthAdapter)

    import asyncio

    asyncio.run(check())


async def test_dangling_credential_id_surfaces_as_401_through_client():
    """AUTH-013 end-to-end: a deleted credential makes the live request
    path fail with the 401-class CredentialUnavailableError — the legacy
    fields on the resource are never resurrected."""
    from core.auth_adapter import CredentialUnavailableError
    from core.credential import CredentialStore, CredentialType
    from core.errors import AuthenticationError

    http = FakeHttp()
    store = CredentialStore()  # empty: the credential was deleted
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    client = GeminiCliClient(backend=make_backend(http), auth=adapter.auth)
    resource = make_resource(credential_id="deleted-credential")

    with pytest.raises(CredentialUnavailableError) as exc_info:
        await client.post(resource, "https://cloudcode-pa.googleapis.com", {})

    assert isinstance(exc_info.value, AuthenticationError)
    assert "deleted-credential" in str(exc_info.value)
    assert http.post_calls == []  # no token endpoint call attempted


# -- AUTH-014: refresh token rotation persistence ---------------------------------


def rotated_response(token: str, rotated: str) -> dict:
    return {
        "access_token": token,
        "expires_in": 3600,
        "refresh_token": rotated,
    }


async def test_gemini_rotation_persists_to_credential():
    """A rotated refresh_token in the refresh response is persisted into
    the bound Credential (only refresh_token changes; client_id/secret
    preserved; no access token/expiry added)."""
    http = FakeHttp()
    http.responses.append(http.ok(rotated_response("token-A", "rotated-rt")))
    http.responses.append(http.ok(rotated_response("token-B", "")))
    store = CredentialStore()
    credential = store.add(oauth_credential())
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    await adapter.get_runtime_credentials(cred, resource)
    await adapter.refresh(cred, resource)

    first_call, second_call = http.post_calls
    assert first_call["data"]["refresh_token"] == "cred-refresh-token"
    assert second_call["data"]["refresh_token"] == "rotated-rt"
    assert credential.payload["refresh_token"] == "rotated-rt"
    assert credential.payload["client_id"] == "cred-client-id"
    assert credential.payload["client_secret"] == "cred-client-secret"
    assert "access_token" not in credential.payload
    assert "expires_at" not in credential.payload
    # legacy Resource fields are never written back
    assert resource.refresh_token == ""


async def test_gemini_no_rotation_keeps_credential_unchanged():
    http = FakeHttp()
    http.responses.append(http.ok(rotated_response("token-A", "")))
    store = CredentialStore()
    credential = store.add(oauth_credential())
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    await adapter.get_runtime_credentials(cred, resource)

    assert credential.payload["refresh_token"] == "cred-refresh-token"


async def test_gemini_rotation_persistence_failure_fails_loudly():
    http = FakeHttp()
    http.responses.append(http.ok(rotated_response("token-A", "rotated-rt")))

    class FailingStore(CredentialStore):
        def update_payload(self, credential_id, payload):
            raise RuntimeError("repository unavailable")

    store = FailingStore()
    store.add(oauth_credential())
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    with pytest.raises(RuntimeError, match="repository unavailable"):
        await adapter.get_runtime_credentials(cred, resource)


async def test_gemini_rotation_without_binding_stays_runtime_only():
    """credential_id=None: rotation stays runtime-only, no repository
    write, legacy behaviour unchanged (resolver path, no explicit
    credential)."""
    http = FakeHttp()
    http.responses.append(http.ok(rotated_response("token-A", "rotated-rt")))
    http.responses.append(http.ok(rotated_response("token-B", "")))
    store = CredentialStore()
    credential = store.add(oauth_credential())  # exists but not referenced
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    legacy_resource = make_resource(credential_id=None)

    await adapter.auth.get_access_token(legacy_resource)
    await adapter.auth.get_access_token(legacy_resource, force=True)

    first_call, second_call = http.post_calls
    assert first_call["data"]["refresh_token"] == "refresh-token-1"
    assert second_call["data"]["refresh_token"] == "rotated-rt"
    # the unreferenced credential was not touched
    assert credential.payload["refresh_token"] == "cred-refresh-token"


async def test_gemini_rotated_token_survives_adapter_rebuild():
    """Simulated restart: a fresh adapter over the same repository uses
    the persisted rotated refresh token (re-fetched from the durable
    source, not the stale in-memory object)."""
    http = FakeHttp()
    http.responses.append(http.ok(rotated_response("token-A", "rotated-rt")))
    store = CredentialStore()
    store.add(oauth_credential())
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    await adapter.refresh(cred, resource)
    assert store.get("google-oauth-01").payload["refresh_token"] == "rotated-rt"

    restarted_http = FakeHttp()
    restarted_http.responses.append(restarted_http.token_ok("token-C"))
    restarted = make_adapter(store, clock=FakeClock(), http=restarted_http)
    fresh_cred = store.require("google-oauth-01")
    await restarted.refresh(fresh_cred, resource)

    (refresh_call,) = restarted_http.post_calls
    assert refresh_call["data"]["refresh_token"] == "rotated-rt"


# -- AUTH-014-FIX-01: persist-before-commit ordering ---------------------------------


class FlakyCredentialStore(CredentialStore):
    """Repository whose update_payload can be toggled to fail."""

    def __init__(self):
        super().__init__()
        self.fail = False

    def update_payload(self, credential_id, payload):
        if self.fail:
            raise RuntimeError("repository unavailable")
        return super().update_payload(credential_id, payload)


async def test_gemini_rotation_failure_leaves_no_runtime_state():
    """Rotated token + repository failure: the exception propagates and
    NO runtime state is committed (no access token, no rotated cache)."""
    http = FakeHttp()
    http.responses.append(http.ok(rotated_response("token-A", "rotated-rt")))
    store = FlakyCredentialStore()
    store.fail = True
    store.add(oauth_credential())
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    with pytest.raises(RuntimeError, match="repository unavailable"):
        await adapter.get_runtime_credentials(cred, resource)

    assert adapter.auth._access_token is None
    assert adapter.auth._expires_at == 0.0
    assert adapter.auth._rotated_refresh_token is None


async def test_gemini_repair_then_refresh_reexecutes():
    """After the repository is repaired, the next call re-runs the whole
    refresh (no reuse of the failed attempt's uncommitted runtime token)."""
    http = FakeHttp()
    http.responses.append(http.ok(rotated_response("token-A", "rotated-rt")))
    store = FlakyCredentialStore()
    store.fail = True
    store.add(oauth_credential())
    adapter = make_adapter(store, clock=FakeClock(), http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    with pytest.raises(RuntimeError):
        await adapter.get_runtime_credentials(cred, resource)

    http.responses.append(http.ok(rotated_response("token-B", "")))
    store.fail = False
    runtime = await adapter.get_runtime_credentials(cred, resource)

    assert runtime.headers == {"Authorization": "Bearer token-B"}
    assert adapter.auth._access_token == "token-B"  # fresh, from re-executed refresh
    assert adapter.auth._rotated_refresh_token is None  # second response: no rotation
    assert len(http.post_calls) == 2  # refresh ran twice (no state reuse)


async def test_gemini_normal_rotation_commits_runtime_after_persistence():
    """Normal rotation: repository write succeeds first, then the runtime
    state (token + rotated cache) is committed and the NEXT refresh uses
    the rotated refresh token."""
    clock = FakeClock()
    http = FakeHttp()
    http.responses.append(http.ok(rotated_response("token-A", "rotated-rt")))
    http.responses.append(http.ok(rotated_response("token-B", "")))
    store = CredentialStore()
    store.add(oauth_credential())
    adapter = make_adapter(store, clock=clock, http=http)
    cred = oauth_credential()
    resource = credentialless_resource()

    await adapter.get_runtime_credentials(cred, resource)
    assert adapter.auth._access_token == "token-A"
    assert adapter.auth._rotated_refresh_token == "rotated-rt"

    # jump past the 180s pre-refresh window (expiry 4600) to force refresh
    clock.value = 4600 - PRE_REFRESH_SECONDS
    await adapter.get_runtime_credentials(cred, resource)
    (first_call, second_call) = http.post_calls
    assert second_call["data"]["refresh_token"] == "rotated-rt"
