"""Antigravity credential migration tests (AUTH-002 mapping + AUTH-006).

Covers the credential→material mapping on the AntigravityAuthAdapter and
the end-to-end request path.  The former "no refresh logic exists" pin
(kept while antigravity was bearer-token-only) is superseded by AUTH-006:
refresh now exists, owned exclusively by the adapter's auth
implementation — see test_auth_adapter.py.
"""

from __future__ import annotations

import pytest

import httpx

from core.credential import Credential, CredentialStore, CredentialType
from providers.antigravity.auth_adapter import AntigravityAuthAdapter
from providers.antigravity.client import AntigravityClient, resource_access_token
from providers.antigravity.provider import AntigravityProvider
from providers.antigravity.resource import AntigravityResource


MODEL_RESPONSE = {"models": {"gemini-test": {"model": "gemini-test"}}}
TOKEN_RESPONSE = {
    "access_token": "cred-access-token",
    "expires_in": 3600,
}


class RoutingBackend:
    """ExecutionBackend double: token endpoint vs API endpoint routing."""

    def __init__(self, token_response=None, api_response=None):
        self.calls = []
        self._token_response = token_response or httpx.Response(200, json=TOKEN_RESPONSE)
        self._api_response = api_response or httpx.Response(200, json=MODEL_RESPONSE)

    async def execute(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if "oauth2.googleapis.com" in url:
            return self._token_response
        return self._api_response

    async def close(self):
        pass


def make_credential(**payload_overrides) -> Credential:
    payload = {
        "refresh_token": "cred-refresh-token",
        "client_id": "cred-client-id",
        "client_secret": "cred-client-secret",
    }
    payload.update(payload_overrides)
    return Credential(
        id="antigravity-oauth-01", type=CredentialType.OAUTH, payload=payload
    )


def make_adapter(store=None) -> AntigravityAuthAdapter:
    return AntigravityAuthAdapter(http=object(), credential_store=store)


# -- legacy client resolver (unchanged) ----------------------------------------


def test_default_token_resolver_reads_legacy_field():
    resource = AntigravityResource(id="a", access_token="legacy-token")
    assert resource_access_token(resource) == "legacy-token"


async def test_client_default_resolver_keeps_legacy_behavior():
    backend = RoutingBackend()
    client = AntigravityClient(backend=backend)

    await client.fetch_available_models(
        AntigravityResource(id="a", access_token="legacy-token")
    )
    api_call = [c for c in backend.calls if "oauth2" not in c["url"]][0]
    assert api_call["headers"]["Authorization"] == "Bearer legacy-token"
    # static-token resources perform no token endpoint call at all
    assert not [c for c in backend.calls if "oauth2" in c["url"]]


# -- material resolution --------------------------------------------------------


def test_provider_material_with_refresh_credential_uses_payload():
    store = CredentialStore()
    store.add(make_credential())
    adapter = make_adapter(store)
    resource = AntigravityResource(id="a", credential_id="antigravity-oauth-01")

    material = adapter.material_for(resource)

    assert material["refresh_token"] == "cred-refresh-token"
    assert material["client_id"] == "cred-client-id"
    assert material["client_secret"] == "cred-client-secret"


def test_provider_material_static_access_token_compat():
    """AUTH-002 compat: a credential carrying only a static access_token
    still resolves (runtime seed), though validate() rejects it."""
    store = CredentialStore()
    store.add(
        Credential(
            id="antigravity-oauth-01",
            type=CredentialType.OAUTH,
            payload={"access_token": "cred-access-token"},
        )
    )
    adapter = make_adapter(store)
    resource = AntigravityResource(id="a", credential_id="antigravity-oauth-01")

    material = adapter.material_for(resource)

    assert material["access_token"] == "cred-access-token"
    assert material["refresh_token"] == ""


def test_provider_material_wrong_type_fails_closed():
    """AUTH-013: credential_id set + wrong type -> no legacy fallback."""
    from core.auth_adapter import CredentialUnavailableError

    store = CredentialStore()
    store.add(
        Credential(id="antigravity-oauth-01", type=CredentialType.API_KEY, payload={})
    )
    adapter = make_adapter(store)
    resource = AntigravityResource(
        id="a", credential_id="antigravity-oauth-01", access_token="legacy-token"
    )

    with pytest.raises(CredentialUnavailableError) as exc_info:
        adapter.material_for(resource)

    # legacy fields on the resource must NOT be used
    assert "legacy-token" not in str(exc_info.value)


def test_provider_material_explicit_credential_wins_over_store():
    store = CredentialStore()
    store.add(make_credential())
    adapter = make_adapter(store)
    resource = AntigravityResource(id="a", credential_id="antigravity-oauth-01")
    explicit = make_credential(refresh_token="explicit-refresh")

    material = adapter.material_from(explicit, resource)

    assert material["refresh_token"] == "explicit-refresh"


# -- request path end-to-end ------------------------------------------------------


async def test_provider_request_uses_credential_refresh_end_to_end():
    """complete() resolves the credential via the adapter, refreshes at
    the token endpoint through the shared backend, then calls the API."""
    backend = RoutingBackend()
    store = CredentialStore()
    store.add(make_credential())
    provider = AntigravityProvider(backend=backend, credential_store=store)
    resource = AntigravityResource(
        id="a",
        credential_id="antigravity-oauth-01",
        project_id="proj",
    )

    await provider.client.fetch_available_models(resource)

    token_calls = [c for c in backend.calls if "oauth2.googleapis.com" in c["url"]]
    api_calls = [c for c in backend.calls if "oauth2.googleapis.com" not in c["url"]]
    assert len(token_calls) == 1
    assert "grant_type=refresh_token" in token_calls[0]["data"].decode("utf-8")
    assert api_calls[0]["headers"]["Authorization"] == "Bearer cred-access-token"


async def test_provider_static_token_resource_makes_no_token_calls():
    """Legacy static-token resources keep the pre-AUTH-006 behavior."""
    backend = RoutingBackend()
    provider = AntigravityProvider(backend=backend)
    resource = AntigravityResource(id="a", access_token="legacy-token", project_id="p")

    await provider.client.fetch_available_models(resource)

    assert not [c for c in backend.calls if "oauth2.googleapis.com" in c["url"]]
    api_calls = [c for c in backend.calls if "oauth2.googleapis.com" not in c["url"]]
    assert api_calls[0]["headers"]["Authorization"] == "Bearer legacy-token"


def test_provider_material_dangling_credential_id_fails_closed():
    """AUTH-013: dangling credential_id -> CredentialUnavailableError;
    the resource's legacy access_token/refresh fields are never used."""
    from core.auth_adapter import CredentialUnavailableError
    from core.errors import AuthenticationError

    store = CredentialStore()  # credential deleted
    adapter = make_adapter(store)
    resource = AntigravityResource(
        id="a",
        credential_id="deleted-credential",
        access_token="legacy-token",
        refresh_token="legacy-rt",
    )

    with pytest.raises(CredentialUnavailableError) as exc_info:
        adapter.material_for(resource)

    assert isinstance(exc_info.value, AuthenticationError)
    assert "deleted-credential" in str(exc_info.value)
    assert "legacy-token" not in str(exc_info.value)
    assert "legacy-rt" not in str(exc_info.value)
