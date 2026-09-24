"""ProviderAuthAdapter contract tests (TASK-AUTH-003).

These tests freeze the adapter contract with test doubles only — no
production provider is migrated here.
"""

from __future__ import annotations

import inspect

import pytest

from core.auth_adapter import (
    CredentialError,
    CredentialRefreshFailure,
    CredentialUnavailableError,
    InvalidCredentialError,
    ProviderAuthAdapter,
    RuntimeCredentials,
)
from core.credential import Credential, CredentialType
from core.errors import AuthenticationError, ProviderError, is_retryable
from core.resource import Resource


class OAuthResource(Resource):
    """Resource subclass carrying identity context the adapter may read."""

    project_id: str | None = None


# -- test doubles (the only "adapters" in AUTH-003) ----------------------------


class FakeOAuthAdapter(ProviderAuthAdapter):
    """Minimal oauth-shaped double: caches one access token in memory."""

    provider_id = "fake-oauth"
    credential_types = frozenset({CredentialType.OAUTH})

    def __init__(self) -> None:
        self.calls: list = []
        self._cached: RuntimeCredentials | None = None
        self.invalidated = 0
        self.fail_refresh = False

    async def validate(self, credential, resource=None):
        self.calls.append(("validate", credential.id, resource))
        self.ensure_supported(credential, resource)
        for key in ("refresh_token", "client_id", "client_secret"):
            if not credential.payload.get(key):
                raise InvalidCredentialError(
                    f"oauth credential '{credential.id}' missing '{key}'",
                    provider=self.provider_id,
                    resource_id=resource.id if resource else None,
                )

    async def get_runtime_credentials(self, credential, resource=None):
        self.calls.append(("get", credential.id, resource))
        self.ensure_supported(credential, resource)
        if self._cached is None:
            self._cached = await self.refresh(credential, resource)
        return self._cached

    async def refresh(self, credential, resource=None):
        self.calls.append(("refresh", credential.id, resource))
        self.ensure_supported(credential, resource)
        if self.fail_refresh:
            raise CredentialRefreshFailure(
                "fake refresh rejected",
                provider=self.provider_id,
            )
        self._cached = RuntimeCredentials(
            headers={"Authorization": "Bearer new-access-token"},
            metadata={"project_id": resource.project_id} if resource else {},
            expires_at=1000.0 + 3600.0,
        )
        return self._cached

    async def invalidate(self, credential, resource=None):
        self.calls.append(("invalidate", credential.id, resource))
        self.invalidated += 1
        self._cached = None


class FakeApiKeyAdapter(ProviderAuthAdapter):
    provider_id = "fake-api-key"
    credential_types = frozenset({CredentialType.API_KEY})

    async def validate(self, credential, resource=None):
        self.ensure_supported(credential, resource)

    async def get_runtime_credentials(self, credential, resource=None):
        self.ensure_supported(credential, resource)
        return RuntimeCredentials(
            headers={"x-api-key": credential.payload["api_key"]}
        )

    async def refresh(self, credential, resource=None):
        raise CredentialRefreshFailure("api_key material never refreshes")

    async def invalidate(self, credential, resource=None):
        pass


class FakeNoneAdapter(ProviderAuthAdapter):
    """Handles type=none: no authentication at all."""

    provider_id = "fake-none"
    credential_types = frozenset({CredentialType.NONE})

    async def validate(self, credential, resource=None):
        self.ensure_supported(credential, resource)

    async def get_runtime_credentials(self, credential, resource=None):
        self.ensure_supported(credential, resource)
        return RuntimeCredentials()

    async def refresh(self, credential, resource=None):
        raise CredentialRefreshFailure("nothing to refresh")

    async def invalidate(self, credential, resource=None):
        pass


def oauth_credential(**payload) -> Credential:
    base = {
        "refresh_token": "rt",
        "client_id": "cid",
        "client_secret": "cs",
    }
    base.update(payload)
    return Credential(id="oauth-01", type=CredentialType.OAUTH, payload=base)


def make_resource(**extra) -> OAuthResource:
    return OAuthResource(id="r1", provider="fake", **extra)


# -- contract shape ------------------------------------------------------------


def test_adapter_is_abstract():
    with pytest.raises(TypeError):
        ProviderAuthAdapter()  # type: ignore[abstract]


def test_contract_exposes_the_four_lifecycle_methods():
    for name in ("validate", "get_runtime_credentials", "refresh", "invalidate"):
        method = getattr(ProviderAuthAdapter, name)
        assert getattr(method, "__isabstractmethod__", False)
        params = list(inspect.signature(method).parameters)
        assert params[1:] == ["credential", "resource"], name


def test_runtime_credentials_has_no_transport_fields():
    assert set(RuntimeCredentials.model_fields) == {
        "headers",
        "metadata",
        "expires_at",
    }
    for forbidden in ("url", "method", "body", "model", "request", "response"):
        assert forbidden not in RuntimeCredentials.model_fields


def test_contract_module_requires_no_transport():
    """The contract must not import or require a transport stack: no httpx,
    no ExecutionBackend, no browser automation."""
    import core.auth_adapter as module

    source = inspect.getsource(module)
    for forbidden in ("import httpx", "playwright", "camoufox"):
        assert forbidden not in source
    # the abstract methods only accept credential/resource — never a
    # client, backend, request or response object
    for name in ("validate", "get_runtime_credentials", "refresh", "invalidate"):
        params = list(inspect.signature(getattr(ProviderAuthAdapter, name)).parameters)
        for forbidden_param in ("client", "backend", "http", "request", "response"):
            assert forbidden_param not in params, (name, forbidden_param)


# -- validate ------------------------------------------------------------------


async def test_validate_accepts_complete_oauth_material():
    adapter = FakeOAuthAdapter()
    await adapter.validate(oauth_credential(), make_resource())  # no raise


async def test_validate_rejects_missing_material():
    adapter = FakeOAuthAdapter()
    with pytest.raises(InvalidCredentialError):
        await adapter.validate(oauth_credential(refresh_token=""))


async def test_validate_rejects_unsupported_credential_type():
    adapter = FakeOAuthAdapter()
    api_key_cred = Credential(
        id="k1", type=CredentialType.API_KEY, payload={"api_key": "key"}
    )
    with pytest.raises(CredentialUnavailableError):
        await adapter.validate(api_key_cred, make_resource())


# -- get_runtime_credentials -----------------------------------------------------


async def test_get_runtime_credentials_returns_headers_metadata_expiry():
    adapter = FakeOAuthAdapter()
    resource = make_resource(project_id="proj-9")
    runtime = await adapter.get_runtime_credentials(oauth_credential(), resource)

    assert runtime.headers["Authorization"] == "Bearer new-access-token"
    assert runtime.metadata["project_id"] == "proj-9"
    assert runtime.expires_at is not None


async def test_get_runtime_credentials_works_without_resource():
    adapter = FakeOAuthAdapter()
    runtime = await adapter.get_runtime_credentials(oauth_credential())
    assert "Authorization" in runtime.headers


async def test_credential_type_compatibility_across_adapters():
    oauth_adapter = FakeOAuthAdapter()
    api_key_adapter = FakeApiKeyAdapter()
    none_adapter = FakeNoneAdapter()

    assert oauth_adapter.supports(oauth_credential())
    assert not oauth_adapter.supports(
        Credential(id="n1", type=CredentialType.NONE)
    )

    # each type has an adapter that accepts it
    assert api_key_adapter.supports(
        Credential(id="k1", type=CredentialType.API_KEY, payload={"api_key": "k"})
    )
    assert none_adapter.supports(Credential(id="n1", type=CredentialType.NONE))

    runtime = await api_key_adapter.get_runtime_credentials(
        Credential(id="k1", type=CredentialType.API_KEY, payload={"api_key": "k"})
    )
    assert runtime.headers == {"x-api-key": "k"}

    empty = await none_adapter.get_runtime_credentials(
        Credential(id="n1", type=CredentialType.NONE)
    )
    assert empty.headers == {}


# -- refresh / invalidate --------------------------------------------------------


async def test_refresh_returns_fresh_runtime_credentials():
    adapter = FakeOAuthAdapter()
    first = await adapter.refresh(oauth_credential(), make_resource())
    second = await adapter.refresh(oauth_credential(), make_resource())
    assert first.headers == second.headers
    assert second.expires_at == first.expires_at
    refresh_calls = [c for c in adapter.calls if c[0] == "refresh"]
    assert len(refresh_calls) == 2


async def test_refresh_failure_raises_credential_refresh_failure():
    adapter = FakeOAuthAdapter()
    adapter.fail_refresh = True
    with pytest.raises(CredentialRefreshFailure):
        await adapter.refresh(oauth_credential())


async def test_invalidate_drops_runtime_cache():
    adapter = FakeOAuthAdapter()
    cred = oauth_credential()
    await adapter.get_runtime_credentials(cred)
    await adapter.invalidate(cred)
    assert adapter._cached is None
    await adapter.get_runtime_credentials(cred)
    assert adapter.invalidated == 1
    # refresh ran twice: once on first get, once after invalidation
    refreshes = [c for c in adapter.calls if c[0] == "refresh"]
    assert len(refreshes) == 2


async def test_invalidate_is_safe_to_repeat():
    adapter = FakeOAuthAdapter()
    cred = oauth_credential()
    await adapter.invalidate(cred)
    await adapter.invalidate(cred)
    assert adapter.invalidated == 2


# -- resource context ------------------------------------------------------------


async def test_resource_is_context_not_permanent_state():
    """The adapter receives credential + resource per call and must not
    turn the resource into stored provider state."""
    adapter = FakeOAuthAdapter()
    resource_a = make_resource(project_id="proj-a")
    resource_b = make_resource(project_id="proj-b")
    cred = oauth_credential()

    runtime_a = await adapter.get_runtime_credentials(cred, resource_a)
    assert runtime_a.metadata["project_id"] == "proj-a"

    # after invalidation the adapter rebuilds runtime material and picks
    # up the NEW resource context passed at that call
    await adapter.invalidate(cred)
    runtime_b = await adapter.get_runtime_credentials(cred, resource_b)
    assert runtime_b.metadata["project_id"] == "proj-b"

    # both calls went through the same adapter instance without storing
    # either resource on it
    assert not any(
        isinstance(stored, Resource) for stored in adapter.__dict__.values()
    )


# -- error semantics -------------------------------------------------------------


def test_adapter_errors_are_authentication_errors():
    for error_type in (
        InvalidCredentialError,
        CredentialRefreshFailure,
        CredentialUnavailableError,
    ):
        error = error_type("boom", provider="fake", resource_id="r1")
        assert isinstance(error, AuthenticationError)
        assert isinstance(error, ProviderError)
        assert error.default_status == 401
        assert is_retryable(error) is False


def test_authentication_failure_reuses_existing_core_error():
    """'AuthenticationFailure' is the existing core AuthenticationError —
    AUTH-003 does not duplicate it."""
    error = AuthenticationError("upstream 401", provider="fake")
    assert not isinstance(error, CredentialError)


# -- no secret leakage -------------------------------------------------------------


def test_runtime_credentials_repr_masks_headers():
    runtime = RuntimeCredentials(
        headers={"Authorization": "Bearer super-secret-token"},
        metadata={"project_id": "proj-1", "refresh_token": "rt-secret"},
        expires_at=4600.0,
    )
    rendered = repr(runtime) + str(runtime) + str(runtime.redacted_dict())
    assert "super-secret-token" not in rendered
    assert "rt-secret" not in rendered
    assert "proj-1" in rendered
    assert "Authorization" in rendered  # header names stay visible
    assert "4600.0" in rendered
