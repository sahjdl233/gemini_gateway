"""Firebase credential migration tests (TASK-AUTH-002).

FirebaseAuth (JWT exchange/cache) is intentionally untouched; only the
source of the long-lived material (api_key / app_id / debug_token) moves
behind a Credential-aware resolver.
"""

from __future__ import annotations

from typing import Any, Dict

from core.credential import Credential, CredentialStore, CredentialType
from providers.firebase.client import FirebaseClient, resource_material
from providers.firebase.provider import FirebaseProvider
from providers.firebase.resource import FirebaseResource


def make_resource(**overrides: Any) -> FirebaseResource:
    base = {
        "id": "firebase-project-01",
        "provider": "firebase",
        "project_id": "proj-1",
        "api_key": "legacy-api-key",
        "app_id": "legacy-app-id",
        "debug_token": "legacy-debug-token",
    }
    base.update(overrides)
    return FirebaseResource.model_validate(base)


def make_credential() -> Credential:
    return Credential(
        id="firebase-cred-01",
        type=CredentialType.API_KEY,
        payload={
            "api_key": "cred-api-key",
            "app_id": "cred-app-id",
            "debug_token": "cred-debug-token",
        },
    )


class NoopAuth:
    async def get_jwt(self, project_id, app_id, api_key, debug_token, *, force=False):
        self.last_args = (project_id, app_id, api_key, debug_token, force)
        return "jwt-1"


class RecordingHttp:
    def __init__(self) -> None:
        self.calls: list = []

    async def post(self, url, *, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})

        class Resp:
            status_code = 200
            content = b"{}"

            def json(self):
                return {}

        return Resp()


# -- material resolution ------------------------------------------------------

def test_default_material_reads_legacy_resource_fields():
    resource = make_resource()
    material = resource_material(resource)
    assert material == {
        "project_id": "proj-1",
        "app_id": "legacy-app-id",
        "api_key": "legacy-api-key",
        "debug_token": "legacy-debug-token",
    }


def test_provider_material_without_credential_uses_legacy_fields():
    provider = FirebaseProvider()
    material = provider._firebase_material(make_resource())
    assert material["api_key"] == "legacy-api-key"


def test_provider_material_with_credential_uses_payload():
    store = CredentialStore()
    store.add(make_credential())
    provider = FirebaseProvider(credential_store=store)
    resource = make_resource(credential_id="firebase-cred-01")

    material = provider._firebase_material(resource)

    assert material["api_key"] == "cred-api-key"
    assert material["app_id"] == "cred-app-id"
    assert material["debug_token"] == "cred-debug-token"


def test_provider_material_project_id_stays_on_resource():
    """project_id is Resource identity (one Project = one Resource); the
    Credential payload never overrides it."""
    store = CredentialStore()
    store.add(
        Credential(
            id="firebase-cred-01",
            type=CredentialType.API_KEY,
            payload={"api_key": "cred-api-key", "project_id": "payload-project"},
        )
    )
    provider = FirebaseProvider(credential_store=store)
    resource = make_resource(credential_id="firebase-cred-01")

    material = provider._firebase_material(resource)

    assert material["project_id"] == "proj-1"


def test_provider_material_payload_only_fills_missing_legacy_fields():
    store = CredentialStore()
    store.add(
        Credential(
            id="firebase-cred-01",
            type=CredentialType.API_KEY,
            payload={"api_key": "cred-api-key"},
        )
    )
    provider = FirebaseProvider(credential_store=store)
    resource = make_resource(credential_id="firebase-cred-01")

    material = provider._firebase_material(resource)

    assert material["api_key"] == "cred-api-key"
    assert material["app_id"] == "legacy-app-id"


def test_provider_material_wrong_type_falls_back_to_legacy():
    store = CredentialStore()
    store.add(Credential(id="firebase-cred-01", type=CredentialType.OAUTH, payload={}))
    provider = FirebaseProvider(credential_store=store)
    material = provider._firebase_material(make_resource(credential_id="firebase-cred-01"))
    assert material["api_key"] == "legacy-api-key"


# -- client threads material into URL/headers/auth -----------------------------

async def test_client_uses_resolved_material_in_url_headers_and_auth():
    http = RecordingHttp()
    auth = NoopAuth()

    def resolver(resource) -> Dict[str, str]:
        return {
            "project_id": "cred-project",
            "app_id": "cred-app-id",
            "api_key": "cred-api-key",
            "debug_token": "cred-debug-token",
        }

    client = FirebaseClient(http=http, auth=auth, material_resolver=resolver)
    await client.complete(make_resource(), "gemini-3.8-flash", {"contents": []})

    (call,) = http.calls
    assert "/projects/cred-project/models/gemini-3.8-flash:generateContent" in call["url"]
    assert call["headers"]["x-goog-api-key"] == "cred-api-key"
    assert call["headers"]["X-Firebase-Appid"] == "cred-app-id"
    assert call["headers"]["X-Firebase-AppCheck"] == "jwt-1"
    assert auth.last_args[:4] == ("cred-project", "cred-app-id", "cred-api-key", "cred-debug-token")


async def test_client_default_resolver_uses_resource_fields():
    http = RecordingHttp()
    auth = NoopAuth()
    client = FirebaseClient(http=http, auth=auth)
    await client.complete(make_resource(), "gemini-3.8-flash", {"contents": []})

    (call,) = http.calls
    assert "/projects/proj-1/models/" in call["url"]
    assert call["headers"]["x-goog-api-key"] == "legacy-api-key"
