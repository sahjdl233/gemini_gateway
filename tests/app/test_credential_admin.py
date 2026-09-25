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
from core.credential_encryption import CredentialEncryptor
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
