"""AUTH-002 wiring: config credentials -> CredentialStore -> providers."""

from __future__ import annotations

from app.main import build_credential_store, build_runtime
from core.credential import CredentialType


def make_config() -> dict:
    return {
        "credentials": [
            {
                "id": "google-oauth-01",
                "type": "oauth",
                "payload": {
                    "refresh_token": "rt",
                    "client_id": "cid",
                    "client_secret": "cs",
                },
            }
        ],
        "providers": {
            "gemini_cli": {
                "enabled": True,
                "resources": [
                    {
                        "id": "cli-1",
                        "credential_id": "google-oauth-01",
                        "project_id": "proj",
                    }
                ],
            },
            "fake": {
                "enabled": True,
                "resources": [{"id": "fake-01", "scenario": "success"}],
            },
        },
    }


def test_build_credential_store_from_config():
    store = build_credential_store(make_config())
    cred = store.get("google-oauth-01")
    assert cred is not None
    assert cred.type is CredentialType.OAUTH
    assert cred.payload["refresh_token"] == "rt"


def test_build_credential_store_empty_config():
    store = build_credential_store({})
    assert len(store) == 0


def test_build_runtime_wires_store_into_providers():
    config = make_config()
    store = build_credential_store(config)
    scheduler = build_runtime(config, store)

    gemini = scheduler.providers["gemini_cli"]
    assert gemini._credential_store is store
    material = gemini._oauth_material(
        scheduler.pools["gemini_cli"].resources[0]
    )
    assert material["refresh_token"] == "rt"

    # fake has no credential support and stays untouched
    assert not hasattr(scheduler.providers["fake"], "set_credential_store")


def test_resource_credential_id_flows_through_factory():
    config = make_config()
    scheduler = build_runtime(config, build_credential_store(config))
    resource = scheduler.pools["gemini_cli"].resources[0]
    assert resource.credential_id == "google-oauth-01"
