"""Application integration for credential persistence modes (AUTH-009).

Verifies explicit startup semantics:

* memory (default)  -> normal startup, unchanged behaviour
* postgres          -> durable repository wired into the runtime
* postgres + no DSN -> startup failure (no silent fallback)
* postgres + unreachable DB -> startup failure
* wrong encryption key -> decryption failure on read (never empty payload)
"""

from __future__ import annotations

import base64
import os

import pytest

from app.main import build_credential_store
from core.credential import Credential, CredentialStore, CredentialType
from core.credential_encryption import CredentialEncryptor
from core.credential_postgres import PostgreSQLCredentialRepository
from tests.core._fake_postgres import FakePostgres


def make_config(**repo_cfg) -> dict:
    config = {
        "credentials": [
            {
                "id": "google-oauth-01",
                "type": "oauth",
                "payload": {"refresh_token": "rt", "client_id": "cid",
                            "client_secret": "cs"},
            }
        ],
    }
    if repo_cfg:
        config["credential_repository"] = repo_cfg
    return config


# -- memory mode (default; unchanged) --------------------------------------------


def test_default_backend_is_memory():
    repo = build_credential_store(make_config())
    assert isinstance(repo, CredentialStore)
    assert repo.get("google-oauth-01").payload["refresh_token"] == "rt"


def test_explicit_memory_backend():
    repo = build_credential_store(make_config(backend="memory"))
    assert isinstance(repo, CredentialStore)


def test_unknown_backend_fails():
    with pytest.raises(ValueError):
        build_credential_store(make_config(backend="oracle"))


# -- postgres mode --------------------------------------------------------------


def test_postgres_backend_requires_database_url(monkeypatch):
    monkeypatch.delenv("GEMINI_GATEWAY_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "GEMINI_GATEWAY_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode()
    )
    with pytest.raises(RuntimeError, match="GEMINI_GATEWAY_DATABASE_URL"):
        build_credential_store(make_config(backend="postgres"))


def test_postgres_backend_unreachable_database_is_startup_failure(monkeypatch):
    """Real psycopg connection attempt against a closed port: the failure
    propagates — create_app must never fall back to an empty store."""
    monkeypatch.setenv(
        "GEMINI_GATEWAY_DATABASE_URL",
        "postgresql://gateway:secret@127.0.0.1:1/none",
    )
    monkeypatch.setenv(
        "GEMINI_GATEWAY_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode()
    )
    import psycopg

    with pytest.raises(psycopg.OperationalError):
        build_credential_store(make_config(backend="postgres"))


def test_postgres_backend_missing_encryption_key_fails(monkeypatch):
    monkeypatch.setenv(
        "GEMINI_GATEWAY_DATABASE_URL",
        "postgresql://gateway:secret@127.0.0.1:1/none",
    )
    monkeypatch.delenv("GEMINI_GATEWAY_ENCRYPTION_KEY", raising=False)
    from core.credential_encryption import CredentialEncryptionConfigError

    # the missing key must fail before any connection attempt matters
    with pytest.raises(CredentialEncryptionConfigError):
        build_credential_store(make_config(backend="postgres"))


# -- runtime wiring: durable repository satisfies the provider seam -----------------


def test_durable_repository_fits_provider_credential_seam(monkeypatch):
    """Providers resolve credentials exclusively via get/require — the
    durable repository drops in without provider changes."""
    from app.main import build_runtime

    fake = FakePostgres()
    repo = PostgreSQLCredentialRepository(
        fake.connection_factory(),
        encryptor=CredentialEncryptor(os.urandom(32)),
    )
    scheduler = build_runtime(
        {
            "credentials": [],
            "providers": {
                "gemini_cli": {
                    "enabled": True,
                    "resources": [
                        {"id": "cli-1", "credential_id": "google-oauth-01",
                         "project_id": "proj"}
                    ],
                }
            },
        },
        repo,
    )
    repo.add(
        Credential(
            id="google-oauth-01",
            type=CredentialType.OAUTH,
            payload={"refresh_token": "rt", "client_id": "cid",
                     "client_secret": "cs"},
        )
    )

    gemini = scheduler.providers["gemini_cli"]
    resource = scheduler.pools["gemini_cli"].resources[0]
    import asyncio

    async def check():
        adapter = await gemini._auth_adapter_for(resource)
        material = adapter.material_for(resource)
        assert material["refresh_token"] == "rt"

    asyncio.run(check())


def test_wrong_key_reads_fail_closed_through_runtime_seam():
    """Wrong GEMINI_GATEWAY_ENCRYPTION_KEY: repository reads raise a
    decryption failure — never an empty payload, never 'not found'."""
    from core.credential_encryption import CredentialDecryptionError

    harness = FakePostgres()
    writer = PostgreSQLCredentialRepository(
        harness.connection_factory(),
        encryptor=CredentialEncryptor(os.urandom(32)),
    )
    writer.add(
        Credential(id="c2", type=CredentialType.OAUTH, payload={"secret": "x"})
    )
    reader = PostgreSQLCredentialRepository(
        harness.connection_factory(),
        encryptor=CredentialEncryptor(os.urandom(32)),  # different key
    )
    with pytest.raises(CredentialDecryptionError):
        reader.get("c2")


# -- AUTH-010: legacy credential migration at startup ----------------------------


def antigravity_legacy_config() -> dict:
    return {
        "credential_repository": {"backend": "postgres"},
        "providers": {
            "antigravity": {
                "enabled": True,
                "resources": [
                    {
                        "id": "ag-01",
                        "refresh_token": "legacy-rt",
                        "client_id": "legacy-cid",
                        "client_secret": "legacy-cs",
                        "project_id": "proj",
                    }
                ],
            }
        },
    }


def test_memory_backend_keeps_original_behaviour(monkeypatch):
    """Memory mode: no migration, resources keep credential_id=None and
    the legacy fields remain the working compatibility path."""
    from app.main import create_app

    config = {
        "providers": {
            "gemini_cli": {
                "enabled": True,
                "resources": [
                    {
                        "id": "cli-01",
                        "refresh_token": "legacy-rt",
                        "client_id": "legacy-cid",
                        "client_secret": "legacy-cs",
                        "project_id": "proj",
                    }
                ],
            }
        }
    }
    app = create_app(config)
    resource = app.state.scheduler.pools["gemini_cli"].resources[0]
    assert resource.credential_id is None
    store = app.state.credential_store
    assert store.get("legacy-gemini_cli-cli-01") is None


def test_postgres_backend_migrates_legacy_resources(monkeypatch):
    """Postgres mode: legacy fields are copied into an encrypted
    Credential and the resource is repointed at credential_id."""
    from app.main import create_app
    from core.credential_encryption import CredentialEncryptor

    harness = FakePostgres()
    repo = PostgreSQLCredentialRepository(
        harness.connection_factory(),
        encryptor=CredentialEncryptor(os.urandom(32)),
    )
    monkeypatch.setattr(
        "app.main.build_credential_store", lambda config: repo
    )

    app = create_app(antigravity_legacy_config())
    resource = app.state.scheduler.pools["antigravity"].resources[0]
    assert resource.credential_id == "legacy-antigravity-ag-01"
    credential = repo.get(resource.credential_id)
    assert credential.payload["refresh_token"] == "legacy-rt"
    # encrypted at rest: no plaintext in the raw row
    assert "legacy-rt" not in str(harness.raw_rows())


def test_postgres_migration_failure_is_startup_failure(monkeypatch):
    """A repository failure during migration must abort startup — never
    silently continue with unresolved credentials."""
    from app.main import create_app
    from core.credential_encryption import CredentialEncryptor

    harness = FakePostgres()
    operation = {"n": 0}

    def flaky_factory():
        operation["n"] += 1
        connection = harness.connection_factory()()
        if operation["n"] >= 2:  # first op = get(), second = the INSERT
            connection.fail_next_execute = RuntimeError("db write failed")
        return connection

    repo = PostgreSQLCredentialRepository(
        flaky_factory, encryptor=CredentialEncryptor(os.urandom(32))
    )
    monkeypatch.setattr(
        "app.main.build_credential_store", lambda config: repo
    )

    with pytest.raises(RuntimeError, match="db write failed"):
        create_app(antigravity_legacy_config())
