"""Credential management API tests (TASK-AUTH-011).

Coverage: auth (503/401), redacted-only responses (no plaintext secrets),
CRUD over the CredentialRepository, error mapping (409 duplicate / 404
unknown / 400 invalid), and persistence-mode parity (memory unchanged;
postgres mode persists encrypted envelopes through the same API).
"""

from __future__ import annotations

import base64
import json
import os

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from core.credential import Credential, CredentialType
from core.credential_encryption import (
    ENCRYPTION_KEY_ID_ENV_VAR,
    ENCRYPTION_KEYS_ENV_VAR,
    CredentialEncryptor,
)
from core.credential_postgres import PostgreSQLCredentialRepository
from tests.core._fake_postgres import FakePostgres

ADMIN = {"Authorization": "Bearer test-admin-secret"}


def make_client(tmp_path, monkeypatch, *, with_token: bool = True, config=None):
    if with_token:
        monkeypatch.setenv("ADMIN_TOKEN", "test-admin-secret")
    else:
        monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    cfg = config or {
        "providers": {
            "fake": {
                "enabled": True,
                "resources": [{"id": "fake-01", "scenario": "success"}],
            }
        }
    }
    return TestClient(create_app(cfg, config_path=tmp_path / "config.yaml"))


def oauth_body(cid: str = "google-oauth-01") -> dict:
    return {
        "id": cid,
        "type": "oauth",
        "payload": {
            "refresh_token": "super-secret-rt",
            "client_id": "public-client-id",
            "client_secret": "super-secret-cs",
        },
    }


# -- auth -----------------------------------------------------------------------


def test_credentials_api_requires_configured_admin_token(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, with_token=False) as client:
        assert client.get("/admin/credentials").status_code == 503
        assert client.get("/admin/credentials/x").status_code == 503
        assert client.post("/admin/credentials", json={}).status_code == 503
        assert (
            client.patch("/admin/credentials/x", json={"payload": {}}).status_code
            == 503
        )
        assert client.delete("/admin/credentials/x").status_code == 503


def test_credentials_api_requires_bearer_token(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        assert client.get("/admin/credentials").status_code == 401
        assert (
            client.post("/admin/credentials", json=oauth_body()).status_code == 401
        )


# -- redaction -------------------------------------------------------------------


def test_list_and_get_return_only_redacted_payload(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        created = client.post("/admin/credentials", json=oauth_body(), headers=ADMIN)
        assert created.status_code == 201

        listing = client.get("/admin/credentials", headers=ADMIN)
        assert listing.status_code == 200
        detail = client.get("/admin/credentials/google-oauth-01", headers=ADMIN)
        assert detail.status_code == 200

        for response in (listing, detail):
            body = response.text
            # plaintext secrets never appear in any API response
            assert "super-secret-rt" not in body
            assert "super-secret-cs" not in body
            # secret-shaped keys are visible and masked
            assert '"refresh_token": "***"' in body or '"refresh_token":"***"' in body
            # non-secret identifiers stay visible
            assert "public-client-id" in body
            assert '"id": "google-oauth-01"' in body or '"id":"google-oauth-01"' in body


def test_created_response_is_redacted_too(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        created = client.post("/admin/credentials", json=oauth_body(), headers=ADMIN)
        assert "super-secret-rt" not in created.text
        assert "public-client-id" in created.text


# -- CRUD + error mapping -----------------------------------------------------------


def test_add_duplicate_returns_409(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        assert client.post("/admin/credentials", json=oauth_body(), headers=ADMIN).status_code == 201
        conflict = client.post("/admin/credentials", json=oauth_body(), headers=ADMIN)
        assert conflict.status_code == 409
        assert client.get("/admin/credentials", headers=ADMIN).json()[0][
            "payload"
        ]["refresh_token"] == "***"  # original untouched


def test_update_payload_returns_redacted_and_maps_404(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        client.post("/admin/credentials", json=oauth_body(), headers=ADMIN)

        patched = client.patch(
            "/admin/credentials/google-oauth-01",
            headers=ADMIN,
            json={"payload": {"refresh_token": "new-secret-rt"}},
        )
        assert patched.status_code == 200
        assert "new-secret-rt" not in patched.text
        assert patched.json()["payload"]["refresh_token"] == "***"

        missing = client.patch(
            "/admin/credentials/missing", headers=ADMIN, json={"payload": {}}
        )
        assert missing.status_code == 404


def test_delete_is_idempotent(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        client.post("/admin/credentials", json=oauth_body(), headers=ADMIN)
        assert (
            client.delete("/admin/credentials/google-oauth-01", headers=ADMIN).status_code
            == 204
        )
        assert client.get("/admin/credentials", headers=ADMIN).json() == []
        # repository contract: unknown id removal is a no-op, still 204
        assert (
            client.delete("/admin/credentials/google-oauth-01", headers=ADMIN).status_code
            == 204
        )


def test_invalid_type_and_missing_id_return_400(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        bad_type = client.post(
            "/admin/credentials",
            headers=ADMIN,
            json={"id": "x", "type": "gemini_cli", "payload": {}},
        )
        assert bad_type.status_code == 400  # no provider-specific types
        no_id = client.post(
            "/admin/credentials", headers=ADMIN, json={"type": "oauth"}
        )
        assert no_id.status_code == 400


def test_get_unknown_returns_404(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        assert (
            client.get("/admin/credentials/missing", headers=ADMIN).status_code == 404
        )


# -- persistence modes ---------------------------------------------------------------


def test_memory_mode_add_stays_in_memory(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        client.post("/admin/credentials", json=oauth_body(), headers=ADMIN)
        # visible through the same repository within the process
        assert client.get("/admin/credentials", headers=ADMIN).json() != []


def test_postgres_mode_persists_encrypted_through_api(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-secret")
    harness = FakePostgres()
    repo = PostgreSQLCredentialRepository(
        harness.connection_factory(),
        encryptor=CredentialEncryptor(os.urandom(32)),
    )
    monkeypatch.setattr("app.main.build_credential_store", lambda config: repo)

    config = {
        "credential_repository": {"backend": "postgres"},
        "providers": {
            "fake": {
                "enabled": True,
                "resources": [{"id": "fake-01", "scenario": "success"}],
            }
        },
    }
    with TestClient(
        create_app(config, config_path=tmp_path / "config.yaml")
    ) as client:
        client.post("/admin/credentials", json=oauth_body(), headers=ADMIN)

        # persisted through the repository into an encrypted envelope
        raw_row = harness.raw_rows()["google-oauth-01"]
        envelope = json.loads(raw_row["payload_encrypted"])
        assert set(envelope) == {"v", "alg", "kid", "nonce", "ciphertext"}
        assert "super-secret-rt" not in str(raw_row)

        # the API reads it back decrypted but responds redacted
        detail = client.get("/admin/credentials/google-oauth-01", headers=ADMIN)
        assert detail.status_code == 200
        assert "super-secret-rt" not in detail.text
        assert detail.json()["payload"]["refresh_token"] == "***"

        # update + delete round-trip against the durable repository
        client.patch(
            "/admin/credentials/google-oauth-01",
            headers=ADMIN,
            json={"payload": {"refresh_token": "rotated"}},
        )
        assert client.get(
            "/admin/credentials/google-oauth-01", headers=ADMIN
        ).json()["payload"]["refresh_token"] == "***"
        client.delete("/admin/credentials/google-oauth-01", headers=ADMIN)
        assert harness.raw_rows() == {}


# -- AUTH-012: key rotation API ----------------------------------------------------


def _postgres_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-secret")
    harness = FakePostgres()
    repo = PostgreSQLCredentialRepository(
        harness.connection_factory(),
        encryptor=CredentialEncryptor(os.urandom(32)),
    )
    monkeypatch.setattr("app.main.build_credential_store", lambda config: repo)
    config = {
        "credential_repository": {"backend": "postgres"},
        "providers": {
            "fake": {
                "enabled": True,
                "resources": [{"id": "fake-01", "scenario": "success"}],
            }
        },
    }
    app = create_app(config, config_path=tmp_path / "config.yaml")
    return app, harness, repo


def test_rotate_key_success_returns_counts_only(tmp_path, monkeypatch):
    app, harness, repo = _postgres_app(tmp_path, monkeypatch)
    client = TestClient(app)
    client.post("/admin/credentials", json=oauth_body(), headers=ADMIN)

    keys = {"default": os.urandom(32), "v2": os.urandom(32)}
    monkeypatch.setenv(ENCRYPTION_KEYS_ENV_VAR, json.dumps(
        {kid: base64.b64encode(k).decode() for kid, k in keys.items()}
    ))
    monkeypatch.setenv(ENCRYPTION_KEY_ID_ENV_VAR, "v2")

    response = client.post("/admin/credentials/rotate-key", headers=ADMIN)

    assert response.status_code == 200
    assert response.json() == {"total": 1, "rotated": 1, "skipped": 0}
    # no credential payload in the rotation response
    assert "super-secret-rt" not in response.text
    # rotated to active kid and decrypts through the same repo
    import json as _json

    assert _json.loads(harness.raw_rows()["google-oauth-01"]["payload_encrypted"])[
        "kid"
    ] == "v2"
    detail = client.get("/admin/credentials/google-oauth-01", headers=ADMIN)
    assert detail.status_code == 200
    assert "super-secret-rt" not in detail.text


def test_rotate_key_without_keyring_config_returns_400(tmp_path, monkeypatch):
    app, harness, repo = _postgres_app(tmp_path, monkeypatch)
    client = TestClient(app)
    monkeypatch.delenv(ENCRYPTION_KEYS_ENV_VAR, raising=False)
    monkeypatch.delenv(ENCRYPTION_KEY_ID_ENV_VAR, raising=False)

    response = client.post("/admin/credentials/rotate-key", headers=ADMIN)

    assert response.status_code == 400


def test_rotate_key_with_invalid_keyring_returns_400(tmp_path, monkeypatch):
    app, harness, repo = _postgres_app(tmp_path, monkeypatch)
    client = TestClient(app)
    monkeypatch.setenv(ENCRYPTION_KEYS_ENV_VAR, "not-json")
    monkeypatch.setenv(ENCRYPTION_KEY_ID_ENV_VAR, "v1")

    response = client.post("/admin/credentials/rotate-key", headers=ADMIN)

    assert response.status_code == 400


def test_rotate_key_in_memory_mode_returns_400(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        monkeypatch.setenv(ENCRYPTION_KEYS_ENV_VAR, json.dumps(
            {"v1": base64.b64encode(os.urandom(32)).decode()}
        ))
        monkeypatch.setenv(ENCRYPTION_KEY_ID_ENV_VAR, "v1")

        response = client.post("/admin/credentials/rotate-key", headers=ADMIN)

        assert response.status_code == 400


def test_rotate_key_requires_admin(tmp_path, monkeypatch):
    app, harness, repo = _postgres_app(tmp_path, monkeypatch)
    client = TestClient(app)
    assert client.post("/admin/credentials/rotate-key").status_code == 401


# -- AUTH-011 fix: POST payload must be a JSON object ---------------------------------


@pytest.mark.parametrize("bad_payload", [None, [], "x", 42])
def test_add_rejects_non_object_payload(tmp_path, monkeypatch, bad_payload):
    with make_client(tmp_path, monkeypatch) as client:
        body = oauth_body()
        body["payload"] = bad_payload

        response = client.post(
            "/admin/credentials", json=body, headers=ADMIN
        )

        assert response.status_code == 400


def test_add_without_payload_returns_400(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        body = {"id": "x", "type": "oauth"}
        assert (
            client.post("/admin/credentials", json=body, headers=ADMIN).status_code
            == 400
        )


# -- AUTH-013: credential deletion protection ---------------------------------------


def _config_with_bound_resource(backend: str | None = None) -> dict:
    """A resource explicitly bound to a defined credential."""
    config = {
        "credentials": [
            {
                "id": "google-oauth-01",
                "type": "oauth",
                "payload": {"refresh_token": "rt", "client_id": "cid",
                            "client_secret": "cs"},
            }
        ],
        "providers": {
            "gemini_cli": {
                "enabled": True,
                "resources": [
                    {"id": "cli-1", "credential_id": "google-oauth-01",
                     "project_id": "proj"}
                ],
            }
        },
    }
    if backend:
        config["credential_repository"] = {"backend": backend}
    return config


def test_delete_referenced_credential_returns_409(tmp_path, monkeypatch):
    """AUTH-013: a credential still referenced by a Resource must not be
    deletable — the resource would be left with a dangling reference."""
    with make_client(tmp_path, monkeypatch, config=_config_with_bound_resource()) as client:
        response = client.delete(
            "/admin/credentials/google-oauth-01", headers=ADMIN
        )
        assert response.status_code == 409
        # the credential survives
        detail = client.get("/admin/credentials/google-oauth-01", headers=ADMIN)
        assert detail.status_code == 200


def test_delete_unreferenced_credential_returns_204(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, config=_config_with_bound_resource()) as client:
        client.post(
            "/admin/credentials",
            headers=ADMIN,
            json={"id": "unused-01", "type": "oauth",
                  "payload": {"refresh_token": "rt"}},
        )
        response = client.delete("/admin/credentials/unused-01", headers=ADMIN)
        assert response.status_code == 204
        assert client.get("/admin/credentials/unused-01", headers=ADMIN).status_code == 404


def test_delete_protection_consistent_in_postgres_mode(tmp_path, monkeypatch):
    app, harness, repo = _postgres_app(tmp_path, monkeypatch)
    client = TestClient(app)
    repo.add(
        Credential(
            id="google-oauth-01",
            type=CredentialType.OAUTH,
            payload={"refresh_token": "rt", "client_id": "cid",
                     "client_secret": "cs"},
        )
    )
    # bind the app's live runtime resource to the durable credential
    resource = app.state.scheduler.pools["fake"].resources[0]
    resource.credential_id = "google-oauth-01"

    referenced = client.delete(
        "/admin/credentials/google-oauth-01", headers=ADMIN
    )
    assert referenced.status_code == 409
    assert repo.get("google-oauth-01") is not None  # survived

    # unbind: the credential becomes deletable (consistent with memory mode)
    resource.credential_id = None
    unreferenced = client.post(
        "/admin/credentials",
        headers=ADMIN,
        json={"id": "unused-01", "type": "oauth", "payload": {"note": "x"}},
    )
    assert unreferenced.status_code == 201
    assert (
        client.delete("/admin/credentials/unused-01", headers=ADMIN).status_code
        == 204
    )


def test_referenced_credential_delete_consistent_after_runtime_binding(tmp_path, monkeypatch):
    """AUTH-010 migration binds resources to legacy-* credentials: those
    are equally protected (reference check uses live scheduler pools)."""
    from app.main import create_app

    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-secret")
    config = {
        "credential_repository": {"backend": "postgres"},
        "providers": {
            "gemini_cli": {
                "enabled": True,
                "resources": [
                    {"id": "cli-1", "refresh_token": "rt", "client_id": "cid",
                     "client_secret": "cs", "project_id": "proj"}
                ],
            }
        },
    }
    harness = FakePostgres()
    repo = PostgreSQLCredentialRepository(
        harness.connection_factory(),
        encryptor=CredentialEncryptor(os.urandom(32)),
    )
    monkeypatch.setattr("app.main.build_credential_store", lambda config: repo)
    app = create_app(config, config_path=tmp_path / "config.yaml")
    client = TestClient(app)

    # migration bound the resource to the stable legacy id
    resource = app.state.scheduler.pools["gemini_cli"].resources[0]
    assert resource.credential_id == "legacy-gemini_cli-cli-1"

    response = client.delete(
        "/admin/credentials/legacy-gemini_cli-cli-1", headers=ADMIN
    )
    assert response.status_code == 409
    assert repo.get("legacy-gemini_cli-cli-1") is not None
