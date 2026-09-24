"""Gemini CLI credential migration tests (TASK-AUTH-002).

Covers the AUTH-002 boundary: long-lived OAuth material may live in a
Credential (referenced via ``resource.credential_id``), while the runtime
access-token cache in ``GeminiCliAuth`` keeps its existing behaviour.
"""

from __future__ import annotations

import pytest

from core.credential import Credential, CredentialStore, CredentialType
from providers.gemini_cli.auth import GeminiCliAuth, resource_material
from providers.gemini_cli.auth_adapter import GeminiCliAuthAdapter
from providers.gemini_cli.provider import GeminiCliProvider

from tests.providers._gemini_cli_fakes import FakeHttp, make_resource


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.value = start

    def time(self) -> float:
        return self.value


def make_credential() -> Credential:
    return Credential(
        id="google-oauth-01",
        type=CredentialType.OAUTH,
        payload={
            "refresh_token": "cred-refresh-token",
            "client_id": "cred-client-id",
            "client_secret": "cred-client-secret",
        },
    )


def make_adapter(store=None, http=None) -> GeminiCliAuthAdapter:
    """Build an adapter the way the Provider does (AUTH-004)."""
    return GeminiCliAuthAdapter(
        http=http if http is not None else FakeHttp(),
        credential_store=store,
    )


def ok_generate(text: str) -> dict:
    return {
        "response": {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}
            ]
        }
    }


# -- material resolution ------------------------------------------------------

def test_default_material_reads_legacy_resource_fields():
    resource = make_resource()
    material = resource_material(resource)
    assert material == {
        "refresh_token": "refresh-token-1",
        "client_id": "client-id-1",
        "client_secret": "client-secret-1",
    }


def test_provider_material_without_credential_uses_legacy_fields():
    adapter = make_adapter()
    resource = make_resource(credential_id=None)
    material = adapter.material_for(resource)
    assert material["refresh_token"] == "refresh-token-1"


def test_provider_material_with_credential_uses_payload():
    store = CredentialStore()
    store.add(make_credential())
    adapter = make_adapter(store)
    resource = make_resource(credential_id="google-oauth-01")

    material = adapter.material_for(resource)

    assert material == {
        "refresh_token": "cred-refresh-token",
        "client_id": "cred-client-id",
        "client_secret": "cred-client-secret",
    }


def test_provider_material_with_wrong_credential_type_falls_back_to_legacy():
    store = CredentialStore()
    store.add(Credential(id="google-oauth-01", type=CredentialType.API_KEY, payload={}))
    adapter = make_adapter(store)
    resource = make_resource(credential_id="google-oauth-01")

    material = adapter.material_for(resource)

    assert material["refresh_token"] == "refresh-token-1"


def test_provider_material_unknown_credential_id_falls_back_to_legacy():
    store = CredentialStore()
    adapter = make_adapter(store)
    resource = make_resource(credential_id="not-registered")

    material = adapter.material_for(resource)

    assert material["refresh_token"] == "refresh-token-1"


def test_provider_material_explicit_credential_wins_over_store():
    """Precedence: explicit credential param > credential_id lookup."""
    store = CredentialStore()
    store.add(make_credential())
    adapter = make_adapter(store)
    resource = make_resource(credential_id="google-oauth-01")
    explicit = Credential(
        id="explicit-01",
        type=CredentialType.OAUTH,
        payload={
            "refresh_token": "explicit-refresh",
            "client_id": "explicit-client-id",
            "client_secret": "explicit-client-secret",
        },
    )

    material = adapter.material_from(explicit, resource)

    assert material["refresh_token"] == "explicit-refresh"


def test_set_credential_store_clears_cached_clients():
    provider = GeminiCliProvider()
    provider._clients["x"] = object()
    provider.set_credential_store(CredentialStore())
    assert provider._clients == {}


# -- auth uses the resolved material -------------------------------------------

async def test_auth_refresh_uses_credential_payload():
    """The refresh POST carries material from the Credential, not the
    (empty) legacy Resource fields."""
    http = FakeHttp()
    http.responses.append(http.token_ok("cred-access-token", 3600))
    auth = GeminiCliAuth(
        http,
        clock=FakeClock(),
        material_resolver=lambda res: {
            "refresh_token": "cred-refresh-token",
            "client_id": "cred-client-id",
            "client_secret": "cred-client-secret",
        },
    )
    # Legacy fields deliberately empty: only the Credential can authenticate.
    resource = make_resource(refresh_token="", client_id="", client_secret="")

    token = await auth.get_access_token(resource)

    assert token == "cred-access-token"
    (refresh_call,) = http.post_calls
    assert refresh_call["data"] == {
        "client_id": "cred-client-id",
        "client_secret": "cred-client-secret",
        "refresh_token": "cred-refresh-token",
        "grant_type": "refresh_token",
    }


async def test_auth_without_material_still_raises_configured_error():
    http = FakeHttp()
    auth = GeminiCliAuth(
        http,
        clock=FakeClock(),
        material_resolver=lambda res: {
            "refresh_token": "",
            "client_id": "cid",
            "client_secret": "cs",
        },
    )
    from providers.gemini_cli.errors import GeminiCliAuthError

    with pytest.raises(GeminiCliAuthError):
        await auth.get_access_token(make_resource())


# -- end-to-end through the provider -------------------------------------------

async def test_complete_uses_credential_material_for_refresh():
    http = FakeHttp()
    http.responses.append(http.token_ok("cred-access-token", 3600))
    http.responses.append(http.ok(ok_generate("hi")))

    store = CredentialStore()
    store.add(make_credential())
    provider = GeminiCliProvider(credential_store=store)
    provider.set_http_client(http)

    from core.models import ChatMessage, ChatRequest

    resource = make_resource(
        credential_id="google-oauth-01",
        refresh_token="",
        client_id="",
        client_secret="",
        access_token="",
    )
    request = ChatRequest(
        model="gemini-2.5-flash",
        messages=[ChatMessage(role="user", content="hi")],
    )
    response = await provider.complete(request, resource)

    assert response.text == "hi"
    refresh_call, api_call = http.post_calls
    assert refresh_call["data"]["refresh_token"] == "cred-refresh-token"
    assert api_call["headers"]["Authorization"] == "Bearer cred-access-token"


async def test_runtime_token_cache_behaviour_unchanged_with_credential():
    """Second request reuses the cached access token (no second refresh)."""
    http = FakeHttp()
    http.responses.append(http.token_ok("cred-access-token", 3600))
    http.responses.append(http.ok(ok_generate("a")))
    http.responses.append(http.ok(ok_generate("b")))

    store = CredentialStore()
    store.add(make_credential())
    provider = GeminiCliProvider(credential_store=store)
    provider.set_http_client(http)

    from core.models import ChatMessage, ChatRequest

    resource = make_resource(
        credential_id="google-oauth-01",
        refresh_token="",
        client_id="",
        client_secret="",
        access_token="",
    )
    request = ChatRequest(
        model="gemini-2.5-flash",
        messages=[ChatMessage(role="user", content="x")],
    )
    await provider.complete(request, resource)
    await provider.complete(request, resource)

    assert len(http.post_calls) == 3  # 1 refresh + 2 api calls
