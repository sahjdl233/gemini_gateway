"""TASK-AUTH-016: App Check exchange 429 must stay a RateLimitError.

Before this task a 429 from the Firebase App Check exchange endpoint was
folded into ``FirebaseAuthError`` -> ``CredentialRefreshFailure`` -> 401,
which told the Scheduler "this credential is dead" instead of "this
resource is cooling down".  The contract is now:

    HTTP 429 -> RateLimitError -> Retry-After -> Scheduler / Cooldown
"""
from __future__ import annotations

import pytest

from core.auth_adapter import CredentialRefreshFailure
from core.cooldown import CooldownManager
from core.errors import RateLimitError, is_retryable
from core.pool import InMemoryPool
from providers.firebase.auth import FirebaseAuth
from providers.firebase.errors import (
    FirebaseAuthError,
    FirebaseNetworkError,
    FirebaseRateLimitError,
    FirebaseTimeoutError,
)
from tests.conftest import FakeClock
from tests.providers._firebase_fakes import FakeHttp, FakeResponse


@pytest.fixture
def fake_http() -> FakeHttp:
    return FakeHttp()


def make_auth(fake: FakeHttp) -> FirebaseAuth:
    return FirebaseAuth(client=fake)


# -- App Check exchange response matrix ------------------------------------


async def test_exchange_200_returns_jwt(fake_http: FakeHttp):
    """Baseline: a healthy exchange endpoint is untouched by this change."""
    auth = make_auth(fake_http)
    fake_http.responses.append(fake_http.exchange_ok(token="jwt-1"))

    assert await auth.get_jwt("proj", "app", "key", "debug") == "jwt-1"


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_exchange_auth_failure_stays_auth_error(fake_http: FakeHttp, status):
    """Non-429 rejections keep their existing auth-failure semantics."""
    auth = make_auth(fake_http)
    fake_http.responses.append(
        FakeResponse(status, content=b'{"error": {"message": "denied"}}')
    )

    with pytest.raises(FirebaseAuthError) as excinfo:
        await auth.get_jwt("proj", "app", "key", "debug")

    assert not isinstance(excinfo.value, RateLimitError)
    assert not is_retryable(excinfo.value)


async def test_exchange_429_with_retry_after_raises_rate_limit(fake_http: FakeHttp):
    auth = make_auth(fake_http)
    fake_http.responses.append(
        FakeResponse(
            429,
            content=b'{"error": {"message": "quota exceeded"}}',
            headers={"retry-after": "42"},
        )
    )

    with pytest.raises(RateLimitError) as excinfo:
        await auth.get_jwt(
            "proj", "app", "key", "debug", resource_id="fb-resource-01"
        )

    exc = excinfo.value
    assert isinstance(exc, FirebaseRateLimitError)
    # TASK-AUTH-016 contract fields + TASK-AUTH-016-FIX-01 resource_id.
    assert exc.provider == "firebase"
    assert exc.resource_id == "fb-resource-01"
    assert exc.scope == "resource"
    assert exc.retry_after == 42.0
    assert exc.default_status == 429
    assert is_retryable(exc)


async def test_exchange_429_without_resource_id_still_rate_limits(
    fake_http: FakeHttp,
):
    """``resource_id`` is optional: legacy callers still get a 429.

    The keyword defaults to None so pre-FIX-01 call sites keep working;
    the error must still be a rate limit rather than degrading into an
    auth failure when no resource context is supplied.
    """
    auth = make_auth(fake_http)
    fake_http.responses.append(
        FakeResponse(429, content=b"{}", headers={"retry-after": "3"})
    )

    with pytest.raises(RateLimitError) as excinfo:
        await auth.get_jwt("proj", "app", "key", "debug")

    exc = excinfo.value
    assert isinstance(exc, FirebaseRateLimitError)
    assert exc.provider == "firebase"
    assert exc.resource_id is None
    assert exc.scope == "resource"
    assert exc.retry_after == 3.0


async def test_exchange_429_retry_after_header_case_insensitive(
    fake_http: FakeHttp,
):
    """``Retry-After`` must parse regardless of header casing."""
    auth = make_auth(fake_http)
    fake_http.responses.append(
        FakeResponse(429, content=b"{}", headers={"Retry-After": "7.5"})
    )

    with pytest.raises(RateLimitError) as excinfo:
        await auth.get_jwt("proj", "app", "key", "debug")

    assert excinfo.value.retry_after == 7.5


async def test_exchange_429_without_retry_after_still_rate_limit(
    fake_http: FakeHttp,
):
    """A 429 with no Retry-After is still a rate limit, just backoff-driven."""
    auth = make_auth(fake_http)
    fake_http.responses.append(FakeResponse(429, content=b"{}"))

    with pytest.raises(RateLimitError) as excinfo:
        await auth.get_jwt("proj", "app", "key", "debug")

    assert excinfo.value.retry_after is None
    assert excinfo.value.default_status == 429


@pytest.mark.parametrize("raw", ["not-a-number", "", "-5"])
async def test_exchange_429_unparsable_retry_after_stays_rate_limit(
    fake_http: FakeHttp, raw
):
    """TASK-AUTH-016: a header parse failure must never become a 401.

    The 429 survives as a rate limit; only the retry hint is dropped.
    """
    auth = make_auth(fake_http)
    fake_http.responses.append(
        FakeResponse(429, content=b"{}", headers={"retry-after": raw})
    )

    with pytest.raises(RateLimitError) as excinfo:
        await auth.get_jwt("proj", "app", "key", "debug")

    assert excinfo.value.retry_after is None


async def test_exchange_429_missing_headers_object(fake_http: FakeHttp):
    """A response without a headers mapping must not break the 429 path."""
    auth = make_auth(fake_http)
    resp = FakeResponse(429, content=b"{}")
    resp.headers = None
    fake_http.responses.append(resp)

    with pytest.raises(RateLimitError) as excinfo:
        await auth.get_jwt("proj", "app", "key", "debug")

    assert excinfo.value.retry_after is None


async def test_exchange_timeout_raises_timeout_error(fake_http: FakeHttp):
    auth = make_auth(fake_http)
    fake_http.raise_exc = TimeoutError("timed out")

    with pytest.raises(FirebaseTimeoutError):
        await auth.get_jwt("proj", "app", "key", "debug")


async def test_exchange_network_error_raises_network_error(fake_http: FakeHttp):
    auth = make_auth(fake_http)
    fake_http.raise_exc = RuntimeError("connection refused")

    with pytest.raises(FirebaseNetworkError) as excinfo:
        await auth.get_jwt("proj", "app", "key", "debug")

    # Transport failures must never be classified as rate limits.
    assert not isinstance(excinfo.value, RateLimitError)


# -- closed loop: 429 -> RateLimitError -> record_rate_limit -> cooldown ---


async def test_exchange_429_reaches_cooldown_through_pool(fake_http: FakeHttp):
    """429 -> RateLimitError -> record_rate_limit() -> cooldown.

    Mirrors exactly the branch the Scheduler takes for a RateLimitError.
    """
    from tests.providers._firebase_fakes import make_resource

    clock = FakeClock()
    cooldown = CooldownManager(now_fn=lambda: clock.now)
    resource = make_resource()
    pool = InMemoryPool(
        provider="firebase", resources=[resource], cooldown=cooldown
    )
    auth = make_auth(fake_http)
    fake_http.responses.append(
        FakeResponse(429, content=b"{}", headers={"retry-after": "30"})
    )

    try:
        await auth.get_jwt(
            resource.project_id,
            resource.app_id,
            resource.api_key,
            resource.debug_token,
        )
    except RateLimitError as exc:
        await pool.record_rate_limit(resource, exc.retry_after)
    else:  # pragma: no cover - the 429 must always raise
        pytest.fail("expected RateLimitError")

    assert cooldown.in_cooldown(resource)
    assert resource.consecutive_failures == 1
    # Retry-After (30s) drove the window; jitter is multiplicative only.
    assert (resource.cooldown_until - clock.now).total_seconds() >= 30.0


async def test_exchange_429_is_never_a_credential_refresh_failure(
    fake_http: FakeHttp,
):
    """A rate limit must never be re-wrapped as a 401 refresh failure.

    Guards the exact regression TASK-AUTH-016 targets: a rate limit that
    becomes ``CredentialRefreshFailure`` looks like a dead credential and
    drives the wrong remediation (disable the credential instead of
    cooling the resource down).
    """
    auth = make_auth(fake_http)
    fake_http.responses.append(
        FakeResponse(429, content=b"{}", headers={"retry-after": "5"})
    )

    with pytest.raises(RateLimitError) as excinfo:
        await auth.get_jwt("proj", "app", "key", "debug")

    assert not isinstance(excinfo.value, CredentialRefreshFailure)


# -- adapter surface: the AUTH-003 contract must also preserve 429 ----------


def _api_key_credential(**overrides):
    from core.credential import Credential, CredentialType

    payload = {
        "api_key": "cred-api-key",
        "app_id": "cred-app-id",
        "debug_token": "cred-debug-token",
    }
    payload.update(overrides)
    return Credential(
        id="firebase-cred-01", type=CredentialType.API_KEY, payload=payload
    )


def _credentialless(resource_id: str):
    from tests.providers._firebase_fakes import make_resource

    return make_resource(
        id=resource_id,
        credential_id="firebase-cred-01",
        api_key="",
        app_id="",
        debug_token="",
    )


@pytest.mark.parametrize("surface", ["get_runtime_credentials", "refresh"])
async def test_adapter_surface_never_wraps_429_as_credential_failure(
    fake_http: FakeHttp,
    surface,
):
    """BOTH adapter entry points must re-raise the exchange 429 untouched.

    ``FirebaseAuth`` raises ``FirebaseRateLimitError``, but the adapter is a
    separate wrapping layer -- a missing ``except RateLimitError: raise``
    there would still hand the Scheduler a 401-class
    ``CredentialRefreshFailure``.  Both surfaces are covered because callers
    use them interchangeably.
    """
    from providers.firebase.auth_adapter import FirebaseAuthAdapter

    fake_http.responses.append(
        FakeResponse(429, content=b"{}", headers={"retry-after": "9"})
    )
    adapter = FirebaseAuthAdapter(http=fake_http)
    resource = _credentialless("fb-resource-42")
    credential = _api_key_credential()

    with pytest.raises(RateLimitError) as excinfo:
        if surface == "refresh":
            await adapter.refresh(credential, resource)
        else:
            await adapter.get_runtime_credentials(credential, resource)

    exc = excinfo.value
    assert not isinstance(exc, CredentialRefreshFailure)
    assert isinstance(exc, FirebaseRateLimitError)
    assert exc.provider == "firebase"
    # FIX-01: the resource actually in use must reach the error.
    assert exc.resource_id == "fb-resource-42"
    assert exc.scope == "resource"
    assert exc.retry_after == 9.0
    assert exc.default_status == 429
    assert is_retryable(exc)


@pytest.mark.parametrize("surface", ["get_runtime_credentials", "refresh"])
async def test_adapter_surface_still_maps_auth_failure_to_401(
    fake_http: FakeHttp,
    surface,
):
    """The 429 carve-out must not disable the genuine auth mapping."""
    from core.errors import AuthenticationError
    from providers.firebase.auth_adapter import FirebaseAuthAdapter

    fake_http.responses.append(FakeResponse(400, content=b"{}"))
    adapter = FirebaseAuthAdapter(http=fake_http)
    resource = _credentialless("fb-resource-42")
    credential = _api_key_credential()

    expected = (
        CredentialRefreshFailure if surface == "refresh" else FirebaseAuthError
    )
    with pytest.raises(expected) as excinfo:
        if surface == "refresh":
            await adapter.refresh(credential, resource)
        else:
            await adapter.get_runtime_credentials(credential, resource)

    assert not isinstance(excinfo.value, RateLimitError)
    assert isinstance(excinfo.value, AuthenticationError)
    assert excinfo.value.default_status == 401
    assert not is_retryable(excinfo.value)


async def test_adapter_repeated_429s_do_not_poison_state(fake_http: FakeHttp):
    """Firebase has no give-up counter; prove throttling stays transient.

    Many consecutive exchange 429s must all surface as rate limits, and a
    later healthy exchange must still succeed with no manual recovery.
    """
    from providers.firebase.auth_adapter import FirebaseAuthAdapter

    for _ in range(6):
        fake_http.responses.append(
            FakeResponse(429, content=b"{}", headers={"retry-after": "5"})
        )
    adapter = FirebaseAuthAdapter(http=fake_http)
    resource = _credentialless("fb-resource-42")
    credential = _api_key_credential()

    for _ in range(6):
        with pytest.raises(RateLimitError):
            await adapter.get_runtime_credentials(credential, resource)

    fake_http.responses.append(fake_http.exchange_ok(token="jwt-recovered"))
    creds = await adapter.get_runtime_credentials(credential, resource)
    assert creds.headers["X-Firebase-AppCheck"] == "jwt-recovered"


# -- FIX-01: resource_id must reach the 429 from every real call path ------


@pytest.mark.parametrize("streaming", [False, True])
async def test_client_propagates_resource_id_onto_429(
    fake_http: FakeHttp,
    streaming,
):
    """The real request path (FirebaseClient) must also carry resource_id.

    The client is a separate call site from the adapter; without it a 429
    raised during an actual completion would reach the Scheduler without a
    resource id even when the adapter path is fully fixed.
    """
    from providers.firebase.client import FirebaseClient
    from tests.providers._firebase_fakes import make_resource

    fake_http.responses.append(
        FakeResponse(429, content=b"{}", headers={"retry-after": "6"})
    )
    client = FirebaseClient(http=fake_http, auth=make_auth(fake_http))
    resource = make_resource(id="fb-client-77", credential_id=None)

    with pytest.raises(RateLimitError) as excinfo:
        if streaming:
            async for _ in client.stream(resource, "gemini-2.5-flash", {"x": 1}):
                pass
        else:
            await client.complete(resource, "gemini-2.5-flash", {"x": 1})

    exc = excinfo.value
    assert not isinstance(exc, CredentialRefreshFailure)
    assert exc.provider == "firebase"
    assert exc.resource_id == "fb-client-77"
    assert exc.scope == "resource"
    assert exc.default_status == 429
