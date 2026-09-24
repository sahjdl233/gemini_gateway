"""Antigravity credential migration tests (TASK-AUTH-002).

Antigravity is CURRENTLY bearer access_token only: no OAuth refresh is
implemented or implied.  These tests only verify that the bearer token
may be resolved from a Credential payload when the resource references
one, and that legacy ``access_token`` resources behave exactly as before.
"""

from __future__ import annotations

import httpx

from core.credential import Credential, CredentialStore, CredentialType
from providers.antigravity.client import AntigravityClient, resource_access_token
from providers.antigravity.provider import AntigravityProvider
from providers.antigravity.resource import AntigravityResource


MODEL_RESPONSE = {"models": {"gemini-test": {"model": "gemini-test"}}}


class RecordingBackend:
    def __init__(self, response=None):
        self.calls = []
        self._response = response or httpx.Response(200, json=MODEL_RESPONSE)

    async def execute(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._response

    async def close(self):
        pass


def make_credential() -> Credential:
    return Credential(
        id="antigravity-oauth-01",
        type=CredentialType.OAUTH,
        payload={"access_token": "cred-access-token"},
    )


# -- token resolution ---------------------------------------------------------

def test_default_token_resolver_reads_legacy_field():
    resource = AntigravityResource(id="a", access_token="legacy-token")
    assert resource_access_token(resource) == "legacy-token"


def test_provider_token_without_credential_uses_legacy_field():
    provider = AntigravityProvider(backend=RecordingBackend())
    resource = AntigravityResource(id="a", access_token="legacy-token")
    assert provider._access_token_for(resource) == "legacy-token"


def test_provider_token_with_credential_uses_payload():
    store = CredentialStore()
    store.add(make_credential())
    provider = AntigravityProvider(backend=RecordingBackend(), credential_store=store)
    resource = AntigravityResource(id="a", credential_id="antigravity-oauth-01")

    assert provider._access_token_for(resource) == "cred-access-token"


def test_provider_token_wrong_type_falls_back_to_legacy():
    store = CredentialStore()
    store.add(
        Credential(id="antigravity-oauth-01", type=CredentialType.API_KEY, payload={})
    )
    provider = AntigravityProvider(backend=RecordingBackend(), credential_store=store)
    resource = AntigravityResource(
        id="a", credential_id="antigravity-oauth-01", access_token="legacy-token"
    )
    assert provider._access_token_for(resource) == "legacy-token"


def test_provider_token_no_refresh_logic_exists():
    """CURRENT boundary: antigravity has bearer tokens only — the resource
    still carries refresh fields, but nothing consumes them."""
    provider = AntigravityProvider(backend=RecordingBackend())
    assert not hasattr(provider, "refresh")
    assert not hasattr(provider.client, "refresh")


# -- request path -------------------------------------------------------------

async def test_client_bearer_token_comes_from_resolver():
    backend = RecordingBackend()
    client = AntigravityClient(
        backend=backend,
        token_resolver=lambda resource: "resolved-token",
    )

    await client.fetch_available_models(AntigravityResource(id="a"))

    (call,) = backend.calls
    assert call["headers"]["Authorization"] == "Bearer resolved-token"


async def test_client_default_resolver_keeps_legacy_behavior():
    backend = RecordingBackend()
    client = AntigravityClient(backend=backend)

    await client.fetch_available_models(
        AntigravityResource(id="a", access_token="legacy-token")
    )
    assert backend.calls[0]["headers"]["Authorization"] == "Bearer legacy-token"

    backend.calls.clear()
    await client.fetch_available_models(AntigravityResource(id="a"))
    assert "Authorization" not in backend.calls[0]["headers"]


async def test_provider_request_uses_credential_token_end_to_end():
    backend = RecordingBackend()
    store = CredentialStore()
    store.add(make_credential())
    provider = AntigravityProvider(backend=backend, credential_store=store)
    resource = AntigravityResource(
        id="a", credential_id="antigravity-oauth-01", project_id="proj"
    )

    await provider.client.fetch_available_models(resource)

    (call,) = backend.calls
    assert call["headers"]["Authorization"] == "Bearer cred-access-token"
