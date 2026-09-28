"""TASK-AUTH-016: token-endpoint 429 must stay a RateLimitError.

Before this task a 429 from the OAuth token endpoint was folded into
``GeminiCliAuthError`` -> ``CredentialRefreshFailure`` -> 401, which told
the Scheduler "this credential is dead" instead of "this resource is
cooling down".  The contract is now:

    HTTP 429 -> RateLimitError -> Retry-After -> Scheduler / Cooldown
"""
from __future__ import annotations

import asyncio

import pytest

from core.auth_adapter import CredentialRefreshFailure
from core.cooldown import CooldownManager
from core.errors import RateLimitError, is_retryable
from core.models import ChatMessage, ChatRequest, ChatResponse, Usage
from core.pool import InMemoryPool
from core.scheduler import Scheduler
from providers.gemini_cli.auth import GeminiCliAuth, MAX_REFRESH_ATTEMPTS
from providers.gemini_cli.errors import (
    GeminiCliAuthError,
    GeminiCliNetworkError,
    GeminiCliRateLimitError,
)
from providers.gemini_cli.provider import GeminiCliProvider
from providers.gemini_cli.resource import GeminiCliResource
from tests.conftest import FakeClock
from tests.providers._gemini_cli_fakes import FakeHttp, make_resource


class StubClock:
    """Injectable clock for GeminiCliAuth (avoids wall-clock coupling)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def time(self) -> float:
        return self.value


# -- token endpoint response matrix ----------------------------------------


async def test_token_endpoint_200_returns_token():
    """Baseline: a healthy token endpoint is untouched by this change."""
    http = FakeHttp()
    http.responses.append(http.token_ok("token-A", 3600))
    auth = GeminiCliAuth(http, clock=StubClock())

    assert await auth.get_access_token(make_resource()) == "token-A"


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_token_endpoint_auth_failure_stays_auth_error(status):
    """Non-429 rejections keep their existing auth-failure semantics."""
    http = FakeHttp()
    http.responses.append(http.err(status, {"error": "invalid_grant"}))
    auth = GeminiCliAuth(http, clock=StubClock())

    with pytest.raises(GeminiCliAuthError) as excinfo:
        await auth.get_access_token(make_resource())

    # An auth failure must NOT be a rate limit, and must not be retryable.
    assert not isinstance(excinfo.value, RateLimitError)
    assert not is_retryable(excinfo.value)


async def test_token_endpoint_429_with_retry_after_raises_rate_limit():
    http = FakeHttp()
    http.responses.append(
        http.err(429, {"error": "slow_down"}, headers={"retry-after": "42"})
    )
    auth = GeminiCliAuth(http, clock=StubClock())
    resource = make_resource()

    with pytest.raises(RateLimitError) as excinfo:
        await auth.get_access_token(resource)

    exc = excinfo.value
    assert isinstance(exc, GeminiCliRateLimitError)
    # TASK-AUTH-016 contract fields.
    assert exc.provider == "gemini_cli"
    assert exc.resource_id == resource.id
    assert exc.retry_after == 42.0
    assert exc.default_status == 429
    assert is_retryable(exc)


async def test_token_endpoint_429_retry_after_header_case_insensitive():
    """``Retry-After`` must parse regardless of header casing."""
    http = FakeHttp()
    http.responses.append(
        http.err(429, {"error": "slow_down"}, headers={"Retry-After": "7.5"})
    )
    auth = GeminiCliAuth(http, clock=StubClock())

    with pytest.raises(RateLimitError) as excinfo:
        await auth.get_access_token(make_resource())

    assert excinfo.value.retry_after == 7.5


async def test_token_endpoint_429_without_retry_after_still_rate_limit():
    """A 429 with no Retry-After is still a rate limit, just backoff-driven."""
    http = FakeHttp()
    http.responses.append(http.err(429, {"error": "slow_down"}))
    auth = GeminiCliAuth(http, clock=StubClock())

    with pytest.raises(RateLimitError) as excinfo:
        await auth.get_access_token(make_resource())

    assert excinfo.value.retry_after is None
    assert excinfo.value.default_status == 429


@pytest.mark.parametrize("raw", ["not-a-number", "", "-5"])
async def test_token_endpoint_429_unparsable_retry_after_stays_rate_limit(raw):
    """TASK-AUTH-016: a header parse failure must never become a 401.

    This is the explicit "parsing failure must not turn a 429 into an
    auth failure" requirement: the 429 survives, only the hint is dropped.
    """
    http = FakeHttp()
    http.responses.append(
        http.err(429, {"error": "slow_down"}, headers={"retry-after": raw})
    )
    auth = GeminiCliAuth(http, clock=StubClock())

    with pytest.raises(RateLimitError) as excinfo:
        await auth.get_access_token(make_resource())

    assert excinfo.value.retry_after is None


async def test_token_endpoint_429_missing_headers_object():
    """A response without a headers mapping must not break the 429 path."""
    http = FakeHttp()
    resp = http.err(429, {"error": "slow_down"})
    resp.headers = None
    http.responses.append(resp)
    auth = GeminiCliAuth(http, clock=StubClock())

    with pytest.raises(RateLimitError) as excinfo:
        await auth.get_access_token(make_resource())

    assert excinfo.value.retry_after is None


async def test_token_endpoint_timeout_raises_network_error():
    http = FakeHttp()
    http.raise_exc = asyncio.TimeoutError("token endpoint timed out")
    auth = GeminiCliAuth(http, clock=StubClock())

    with pytest.raises(GeminiCliNetworkError):
        await auth.get_access_token(make_resource())


async def test_token_endpoint_network_error_raises_network_error():
    http = FakeHttp()
    http.raise_exc = ConnectionError("connection reset")
    auth = GeminiCliAuth(http, clock=StubClock())

    with pytest.raises(GeminiCliNetworkError):
        await auth.get_access_token(make_resource())


async def test_repeated_429_never_burns_the_give_up_budget():
    """TASK-AUTH-016: throttling must not disable the credential.

    ``get_access_token`` gives up with a 401-class ``GeminiCliAuthError``
    after MAX_REFRESH_ATTEMPTS failed refreshes.  A 429 is throttling, not a
    broken credential, so it must NOT consume that budget: otherwise a burst
    of token-endpoint throttling would permanently brick the resource, and
    the 4th 429 would surface as the exact 401-class error this task exists
    to eliminate.
    """
    http = FakeHttp()
    for _ in range(MAX_REFRESH_ATTEMPTS + 2):
        http.responses.append(
            http.err(429, {"error": "slow_down"}, headers={"retry-after": "30"})
        )
    auth = GeminiCliAuth(http, clock=StubClock())
    resource = make_resource()

    # Every attempt, well past the give-up cap, stays a rate limit.
    for attempt in range(1, MAX_REFRESH_ATTEMPTS + 3):
        with pytest.raises(RateLimitError) as excinfo:
            await auth.get_access_token(resource)
        assert excinfo.value.retry_after == 30.0, (
            f"attempt {attempt} lost its rate-limit semantics"
        )
        assert not isinstance(excinfo.value, GeminiCliAuthError)


async def test_429_does_not_mask_a_later_real_auth_failure():
    """The give-up budget must still work for genuine auth failures.

    Guards the other side: excluding 429 from the counter must not disable
    the safety cap for real refresh rejections.
    """
    http = FakeHttp()
    # Two throttles that must NOT count...
    for _ in range(2):
        http.responses.append(
            http.err(429, {"error": "slow_down"}, headers={"retry-after": "1"})
        )
    # ...then genuine invalid_grant failures that must count.
    for _ in range(MAX_REFRESH_ATTEMPTS + 1):
        http.responses.append(http.err(400, {"error": "invalid_grant"}))
    auth = GeminiCliAuth(http, clock=StubClock())
    resource = make_resource()

    # The throttles surface as rate limits and leave the budget untouched.
    for _ in range(2):
        with pytest.raises(RateLimitError):
            await auth.get_access_token(resource)

    # Every genuine invalid_grant still counts toward the cap.
    for _ in range(MAX_REFRESH_ATTEMPTS):
        with pytest.raises(GeminiCliAuthError):
            await auth.get_access_token(resource)

    # The cap still trips on the next call, and still as an auth error.
    with pytest.raises(GeminiCliAuthError) as excinfo:
        await auth.get_access_token(resource)
    assert not isinstance(excinfo.value, RateLimitError)


async def test_real_auth_failure_still_consumes_exactly_one_unit_of_budget():
    """A genuine refresh rejection must still increment the give-up counter.

    Companion to ``test_429_does_not_mask_a_later_real_auth_failure``.
    That test only shows the cap eventually trips; it would still pass if a
    real rejection consumed MORE than one unit.  This test pins the exact
    count so removing the ``_consecutive_refresh_failures`` increment (the
    tempting over-correction when fixing TASK-AUTH-016) is caught here.
    """
    http = FakeHttp()
    auth = GeminiCliAuth(http, clock=StubClock())
    resource = make_resource()

    # Each invalid_grant is one budget unit.
    http.responses.append(http.err(400, {"error": "invalid_grant"}))
    with pytest.raises(GeminiCliAuthError):
        await auth.get_access_token(resource)
    assert auth._consecutive_refresh_failures == 1

    # The counter grows one unit at a time, so the cap trips on exactly the
    # MAX_REFRESH_ATTEMPTS-th rejection -- not earlier, not later.
    for expected in range(2, MAX_REFRESH_ATTEMPTS + 1):
        http.responses.append(http.err(400, {"error": "invalid_grant"}))
        with pytest.raises(GeminiCliAuthError):
            await auth.get_access_token(resource)
        assert auth._consecutive_refresh_failures == expected

    # Budget exhausted: the next call gives up without touching the network.
    consumed = len(http.responses)
    with pytest.raises(GeminiCliAuthError):
        await auth.get_access_token(resource)
    assert len(http.responses) == consumed


async def test_repeated_429s_never_accumulate_budget_and_200_recovers():
    """Throttling must not poison the cache: a later 200 still succeeds.

    Proves the counter stays at zero across an arbitrary burst of 429s (not
    merely 'below the cap') and that the credential remains usable as soon
    as the token endpoint stops throttling.
    """
    http = FakeHttp()
    for _ in range(MAX_REFRESH_ATTEMPTS * 4):
        http.responses.append(
            http.err(429, {"error": "slow_down"}, headers={"retry-after": "5"})
        )
    auth = GeminiCliAuth(http, clock=StubClock())
    resource = make_resource()

    for _ in range(MAX_REFRESH_ATTEMPTS * 4):
        with pytest.raises(RateLimitError):
            await auth.get_access_token(resource)
    assert auth._consecutive_refresh_failures == 0

    http.responses.append(
        http.ok({"access_token": "recovered", "expires_in": 3600})
    )
    assert await auth.get_access_token(resource) == "recovered"
    assert auth._consecutive_refresh_failures == 0


# -- adapter surface: the AUTH-003 contract must also preserve 429 ----------


def _oauth_credential(**overrides):
    from core.credential import Credential, CredentialType

    payload = {
        "refresh_token": "cred-refresh-token",
        "client_id": "cred-client-id",
        "client_secret": "cred-client-secret",
    }
    payload.update(overrides)
    return Credential(
        id="google-oauth-01", type=CredentialType.OAUTH, payload=payload
    )


@pytest.mark.parametrize("surface", ["get_runtime_credentials", "refresh"])
async def test_adapter_surface_never_wraps_429_as_credential_failure(surface):
    """BOTH adapter entry points must re-raise the 429 untouched.

    The provider implementation already raises RateLimitError, but the
    adapter is a separate wrapping layer: a single missing ``except
    RateLimitError: raise`` clause there would still surface a 401-class
    CredentialRefreshFailure to the Scheduler.  Both surfaces are covered
    because callers use them interchangeably.
    """
    from providers.gemini_cli.auth_adapter import GeminiCliAuthAdapter

    http = FakeHttp()
    http.responses.append(
        http.err(429, {"error": "slow_down"}, headers={"retry-after": "12"})
    )
    adapter = GeminiCliAuthAdapter(http=http, clock=StubClock())
    resource = make_resource(credential_id="google-oauth-01")
    credential = _oauth_credential()

    with pytest.raises(RateLimitError) as excinfo:
        if surface == "refresh":
            await adapter.refresh(credential, resource)
        else:
            await adapter.get_runtime_credentials(credential, resource)

    exc = excinfo.value
    assert not isinstance(exc, CredentialRefreshFailure)
    assert exc.default_status == 429
    assert exc.retry_after == 12.0
    assert is_retryable(exc)


@pytest.mark.parametrize("surface", ["get_runtime_credentials", "refresh"])
async def test_adapter_surface_still_maps_auth_failure_to_401(surface):
    """The 429 carve-out must not disable the genuine auth mapping.

    Both surfaces must keep a real credential rejection in the 401 class.
    ``refresh`` normalises it to ``CredentialRefreshFailure``;
    ``get_runtime_credentials`` lets the provider's own
    ``GeminiCliAuthError`` (itself an ``AuthenticationError``) through —
    the distinction is pre-existing AUTH-004 behaviour and is pinned here
    only so the 429 carve-out cannot silently widen into a success or a
    rate limit.
    """
    from core.errors import AuthenticationError
    from providers.gemini_cli.auth_adapter import GeminiCliAuthAdapter

    http = FakeHttp()
    http.responses.append(http.err(400, {"error": "invalid_grant"}))
    adapter = GeminiCliAuthAdapter(http=http, clock=StubClock())
    resource = make_resource(credential_id="google-oauth-01")
    credential = _oauth_credential()

    expected = (
        CredentialRefreshFailure
        if surface == "refresh"
        else GeminiCliAuthError
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


async def test_adapter_429_burst_never_bricks_the_credential():
    """End-to-end on the contract surface: throttling must not give up.

    Without the 429 exclusion in the failure counter, a burst longer than
    MAX_REFRESH_ATTEMPTS drives the adapter into the 'giving up' 401-class
    error -- the exact regression this task removes.
    """
    from providers.gemini_cli.auth_adapter import GeminiCliAuthAdapter

    http = FakeHttp()
    for _ in range(MAX_REFRESH_ATTEMPTS + 2):
        http.responses.append(
            http.err(429, {"error": "slow_down"}, headers={"retry-after": "3"})
        )
    adapter = GeminiCliAuthAdapter(http=http, clock=StubClock())
    resource = make_resource(credential_id="google-oauth-01")
    credential = _oauth_credential()

    for _ in range(MAX_REFRESH_ATTEMPTS + 2):
        with pytest.raises(RateLimitError):
            await adapter.get_runtime_credentials(credential, resource)

    http.responses.append(http.token_ok(token="fresh-token"))
    creds = await adapter.get_runtime_credentials(credential, resource)
    assert creds.headers["Authorization"] == "Bearer fresh-token"


def _cli_resource(resource_id: str) -> GeminiCliResource:
    return GeminiCliResource(
        id=resource_id,
        provider="gemini_cli",
        refresh_token="rt-" + resource_id,
        client_id="cid-" + resource_id,
        client_secret="cs-" + resource_id,
        project_id="proj-" + resource_id,
    )


# -- closed loop: 429 -> RateLimitError -> record_rate_limit -> cooldown ---


async def test_429_reaches_cooldown_through_scheduler_closed_loop():
    """429 -> RateLimitError -> Scheduler.record_rate_limit() -> cooldown.

    End-to-end proof that a token-endpoint 429 reaches the cooldown
    machinery unchanged and that its Retry-After drives the window.
    """
    clock = FakeClock()
    cooldown = CooldownManager(now_fn=lambda: clock.now)
    throttled = _cli_resource("cli-throttled")
    healthy = _cli_resource("cli-healthy")
    pool = InMemoryPool(
        provider="gemini_cli",
        resources=[throttled, healthy],
        cooldown=cooldown,
    )

    token_http = FakeHttp()
    token_http.responses.append(
        token_http.err(
            429, {"error": "slow_down"}, headers={"retry-after": "30"}
        )
    )

    class TokenEndpoint429Provider(GeminiCliProvider):
        """Provider whose complete() does nothing but refresh the token."""

        def __init__(self):
            super().__init__(models=["gemini-2.5-flash"])
            self.seen: RateLimitError | None = None

        async def complete(self, request, resource):
            auth = GeminiCliAuth(token_http, clock=StubClock())
            try:
                await auth.get_access_token(resource)
            except RateLimitError as exc:
                # Record exactly what the auth layer produced.
                self.seen = exc
                raise

    provider = TokenEndpoint429Provider()
    scheduler = Scheduler(
        providers={"gemini_cli": provider},
        pools={"gemini_cli": pool},
        max_retries=0,
    )
    request = ChatRequest(
        model="gemini-2.5-flash",
        messages=[ChatMessage(role="user", content="hi")],
    )

    with pytest.raises(RateLimitError) as excinfo:
        await scheduler.chat_completion(request)

    # What escaped the Provider is the token-endpoint error, NOT a
    # credential/401 error.
    escaped = excinfo.value
    assert not isinstance(escaped, CredentialRefreshFailure)
    assert escaped.default_status == 429
    assert escaped.retry_after == 30.0
    assert provider.seen is not None
    assert provider.seen.resource_id in (throttled.id, healthy.id)

    # The Scheduler routed it to record_rate_limit() -> COOLDOWN, and the
    # Retry-After (30s) drove the window (jitter is multiplicative only).
    cooled = throttled if cooldown.in_cooldown(throttled) else healthy
    assert cooled is not healthy or healthy.consecutive_failures > 0
    assert cooldown.in_cooldown(cooled)
    assert cooled.consecutive_failures == 1
    window = (cooled.cooldown_until - clock.now).total_seconds()
    assert window >= 30.0


async def test_429_without_retry_after_uses_pool_record_rate_limit():
    """Same closed loop without Retry-After: backoff drives the cooldown."""
    clock = FakeClock()
    cooldown = CooldownManager(
        now_fn=lambda: clock.now, base_delay=60.0, max_delay=600.0
    )
    resource = _cli_resource("cli-1")
    pool = InMemoryPool(
        provider="gemini_cli", resources=[resource], cooldown=cooldown
    )
    http = FakeHttp()
    http.responses.append(http.err(429, {"error": "slow_down"}))
    auth = GeminiCliAuth(http, clock=StubClock())

    # Exactly the branch Scheduler._chat_completion takes on a
    # ProviderError, exercised directly against the auth layer.
    try:
        await auth.get_access_token(resource)
    except RateLimitError as exc:
        await pool.record_rate_limit(resource, exc.retry_after)
    else:  # pragma: no cover - the 429 must always raise
        pytest.fail("expected RateLimitError")

    assert cooldown.in_cooldown(resource)
    assert (resource.cooldown_until - clock.now).total_seconds() >= 60.0
