"""Legacy credential migration tests (TASK-AUTH-010).

Runs against both the in-memory store and the PostgreSQL repository
backed by the fake driver; the PostgreSQL raw-storage assertions prove
encrypted-at-rest migration without a live server.
"""

from __future__ import annotations

import json
import os

import pytest

from core.credential import Credential, CredentialStore, CredentialType
from core.credential_encryption import CredentialEncryptor
from core.credential_migration import (
    legacy_credential_id,
    migrate_legacy_resource_credentials,
)
from core.credential_postgres import PostgreSQLCredentialRepository
from providers.antigravity.resource import AntigravityResource
from providers.firebase.resource import FirebaseResource
from providers.gemini_cli.resource import GeminiCliResource
from tests.core._fake_postgres import FakePostgres


def make_postgres_repo(fake: FakePostgres):
    return PostgreSQLCredentialRepository(
        fake.connection_factory(),
        encryptor=CredentialEncryptor(os.urandom(32)),
    )


def gemini_legacy_resource(**overrides):
    base = {
        "id": "cli-01",
        "refresh_token": "legacy-rt",
        "client_id": "legacy-cid",
        "client_secret": "legacy-cs",
        "access_token": "legacy-static-at",  # runtime-only: must NOT migrate
        "project_id": "proj",
    }
    base.update(overrides)
    return GeminiCliResource.model_validate(base)


def firebase_legacy_resource(**overrides):
    base = {
        "id": "fb-01",
        "project_id": "proj",
        "api_key": "legacy-api-key",
        "app_id": "legacy-app-id",
        "debug_token": "legacy-debug-token",
    }
    base.update(overrides)
    return FirebaseResource.model_validate(base)


def antigravity_legacy_resource(**overrides):
    base = {
        "id": "ag-01",
        "refresh_token": "legacy-ag-rt",
        "client_id": "legacy-ag-cid",
        "client_secret": "legacy-ag-cs",
        "access_token": "legacy-ag-static-at",  # AUTH-006 compat durable seed
        "project_id": "proj",
    }
    base.update(overrides)
    return AntigravityResource.model_validate(base)


# -- provider mappings ------------------------------------------------------------


def test_gemini_cli_legacy_migrates_to_oauth_credential():
    repo = CredentialStore()
    resource = gemini_legacy_resource()

    migrated = migrate_legacy_resource_credentials(repo, [resource])

    assert migrated == 1
    assert resource.credential_id == "legacy-gemini_cli-cli-01"
    credential = repo.get(resource.credential_id)
    assert credential.type is CredentialType.OAUTH
    assert credential.payload == {
        "refresh_token": "legacy-rt",
        "client_id": "legacy-cid",
        "client_secret": "legacy-cs",
    }


def test_gemini_cli_runtime_access_token_never_migrates():
    """Runtime material stays out of the durable Credential: the legacy
    static access_token on a gemini_cli resource is runtime cache input
    (AUTH-004) and must not enter the repository."""
    repo = CredentialStore()
    resource = gemini_legacy_resource()  # carries access_token

    migrate_legacy_resource_credentials(repo, [resource])

    payload = repo.get(resource.credential_id).payload
    assert "access_token" not in payload
    assert "expires_at" not in payload
    assert "jwt" not in payload


def test_firebase_legacy_migrates_to_api_key_credential():
    repo = CredentialStore()
    resource = firebase_legacy_resource()

    migrate_legacy_resource_credentials(repo, [resource])

    assert resource.credential_id == "legacy-firebase-fb-01"
    credential = repo.get(resource.credential_id)
    assert credential.type is CredentialType.API_KEY
    assert credential.payload == {
        "api_key": "legacy-api-key",
        "app_id": "legacy-app-id",
        "debug_token": "legacy-debug-token",
    }
    # project_id stays Resource identity, never credential material
    assert "project_id" not in credential.payload


def test_antigravity_legacy_migrates_with_compat_access_token():
    repo = CredentialStore()
    resource = antigravity_legacy_resource()

    migrate_legacy_resource_credentials(repo, [resource])

    assert resource.credential_id == "legacy-antigravity-ag-01"
    credential = repo.get(resource.credential_id)
    assert credential.type is CredentialType.OAUTH
    assert credential.payload == {
        "refresh_token": "legacy-ag-rt",
        "client_id": "legacy-ag-cid",
        "client_secret": "legacy-ag-cs",
        # AUTH-006 compat durable seed: static-token resources keep
        # working through the credential path only with this field.
        "access_token": "legacy-ag-static-at",
    }


def test_anonymous_and_empty_resources_are_skipped():
    repo = CredentialStore()
    anon = type(
        "R", (), {"provider": "anonymous_vertex", "id": "a1", "credential_id": None}
    )()
    empty = gemini_legacy_resource(refresh_token="", client_id="",
                                   client_secret="", access_token="")

    migrated = migrate_legacy_resource_credentials(repo, [anon, empty])

    assert migrated == 0
    assert anon.credential_id is None
    assert empty.credential_id is None
    assert len(repo) == 0


# -- existing credential_id / idempotency ---------------------------------------------


def test_resource_with_existing_credential_id_is_untouched():
    repo = CredentialStore()
    resource = gemini_legacy_resource(credential_id="user-defined-01")

    migrated = migrate_legacy_resource_credentials(repo, [resource])

    assert migrated == 0
    assert resource.credential_id == "user-defined-01"
    assert len(repo) == 0  # nothing created


def test_migration_is_idempotent_across_restarts():
    """Repeated startups reuse the same stable id and never overwrite."""
    resource = gemini_legacy_resource()

    repo_1 = CredentialStore()
    migrate_legacy_resource_credentials(repo_1, [resource])
    first_payload = repo_1.get(resource.credential_id).payload
    first_id = resource.credential_id

    # "restart": fresh repository + same resource (mutated credentials,
    # e.g. the user rotated the token in config between runs)
    repo_2 = CredentialStore()
    resource_after_restart = gemini_legacy_resource(refresh_token="changed-rt")
    migrated = migrate_legacy_resource_credentials(repo_2, [resource_after_restart])

    assert migrated == 1
    assert resource_after_restart.credential_id == first_id  # same stable id
    assert repo_2.get(first_id).payload["refresh_token"] == "changed-rt"

    # re-run against the SAME repository: reuse, no duplicate, no overwrite
    migrated_again = migrate_legacy_resource_credentials(repo_2, [resource_after_restart])
    assert migrated_again == 0
    assert len(repo_2) == 1
    assert repo_2.get(first_id).payload["refresh_token"] == "changed-rt"


def test_existing_credential_is_reused_not_overwritten():
    """A credential already stored under the stable id wins: migration
    only repoints the resource."""
    repo = CredentialStore()
    stable_id = legacy_credential_id("gemini_cli", "cli-01")
    repo.add(
        Credential(
            id=stable_id,
            type=CredentialType.OAUTH,
            payload={"refresh_token": "already-stored"},
        )
    )
    resource = gemini_legacy_resource()

    migrated = migrate_legacy_resource_credentials(repo, [resource])

    assert migrated == 0
    assert resource.credential_id == stable_id
    assert repo.get(stable_id).payload == {"refresh_token": "already-stored"}


# -- PostgreSQL: encrypted at rest ------------------------------------------------------


def test_postgres_migration_encrypts_payload_at_rest():
    fake = FakePostgres()
    repo = make_postgres_repo(fake)
    resource = gemini_legacy_resource()

    migrate_legacy_resource_credentials(repo, [resource])

    raw_row = fake.raw_rows()["legacy-gemini_cli-cli-01"]
    envelope = json.loads(raw_row["payload_encrypted"])
    assert set(envelope) == {"v", "alg", "kid", "nonce", "ciphertext"}
    rendered = str(raw_row)
    for secret in ("legacy-rt", "legacy-cs", "legacy-cid"):
        assert secret not in rendered

    # repository read path decrypts back to the migrated payload
    assert repo.get(resource.credential_id).payload["refresh_token"] == "legacy-rt"


# -- failure semantics ---------------------------------------------------------------------


def test_repository_failure_propagates():
    class FailingRepo(CredentialStore):
        def add(self, credential):
            raise RuntimeError("database write failed")

    repo = FailingRepo()
    with pytest.raises(RuntimeError, match="database write failed"):
        migrate_legacy_resource_credentials(repo, [gemini_legacy_resource()])


# -- runtime prefers the credential path after migration -------------------------------------


def test_migrated_resource_resolves_via_credential_path():
    """Post-migration, the provider adapter resolves material from the
    Credential; legacy fields remain as the compatibility path."""
    from providers.gemini_cli.auth_adapter import GeminiCliAuthAdapter
    from tests.providers._gemini_cli_fakes import FakeHttp

    repo = CredentialStore()
    resource = gemini_legacy_resource()
    migrate_legacy_resource_credentials(repo, [resource])

    adapter = GeminiCliAuthAdapter(http=FakeHttp(), credential_store=repo)
    material = adapter.material_for(resource)

    assert material == {
        "refresh_token": "legacy-rt",
        "client_id": "legacy-cid",
        "client_secret": "legacy-cs",
    }
