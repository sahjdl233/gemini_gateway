"""CredentialRepository contract tests (TASK-AUTH-008).

The suite is written against the :class:`CredentialRepository` contract,
parameterized over implementations.  Today the only implementation is
the in-memory ``CredentialStore``; AUTH-009's durable repository must
satisfy the same contract without provider changes.

Contract under test (frozen semantics):
    add    -> duplicate id raises DuplicateCredentialError
    get    -> None for unknown ids
    require-> UnknownCredentialError for unknown ids
    update -> UnknownCredentialError when missing; replaces payload,
              bumps updated_at
    remove -> idempotent for unknown ids
    list   -> insertion order
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest

from core.credential import (
    Credential,
    CredentialRepository,
    CredentialStore,
    CredentialType,
    DuplicateCredentialError,
    UnknownCredentialError,
)
from core.credential_encryption import CredentialEncryptor
from core.credential_postgres import PostgreSQLCredentialRepository
from tests.core._fake_postgres import FakePostgres


def make_in_memory_repo() -> CredentialRepository:
    return CredentialStore()


def make_postgres_fake_repo() -> CredentialRepository:
    fake = FakePostgres()
    return PostgreSQLCredentialRepository(
        fake.connection_factory(),
        encryptor=CredentialEncryptor(os.urandom(32)),
    )


IMPLEMENTATIONS = [make_in_memory_repo, make_postgres_fake_repo]


@pytest.fixture(params=IMPLEMENTATIONS, ids=["in-memory", "postgres-fake"])
def repo(request) -> CredentialRepository:
    return request.param()


def oauth_credential(cid: str = "c1", **payload) -> Credential:
    base = {"refresh_token": "rt-令牌", "client_id": "cid", "client_secret": "cs"}
    base.update(payload)
    return Credential(id=cid, type=CredentialType.OAUTH, payload=base)


def api_key_credential(cid: str = "k1") -> Credential:
    return Credential(
        id=cid,
        type=CredentialType.API_KEY,
        payload={"api_key": "AIzaSyX", "app_id": "app", "debug_token": "dbg"},
    )


# -- CRUD ---------------------------------------------------------------------


def test_store_is_a_credential_repository():
    assert isinstance(CredentialStore(), CredentialRepository)


def test_add_and_get_round_trip(repo):
    credential = repo.add(oauth_credential())
    loaded = repo.get("c1")
    # durable implementations return a reconstructed object: the contract
    # is semantic equality, not object identity
    assert loaded.id == credential.id
    assert loaded.type == credential.type
    assert loaded.payload == credential.payload


def test_get_unknown_returns_none(repo):
    assert repo.get("missing") is None


def test_require_unknown_raises(repo):
    with pytest.raises(UnknownCredentialError):
        repo.require("missing")


def test_require_known_returns_credential(repo):
    repo.add(oauth_credential())
    assert repo.require("c1").id == "c1"


def test_update_payload_replaces_and_bumps_updated_at(repo):
    credential = repo.add(oauth_credential())
    before = credential.updated_at
    updated = repo.update_payload("c1", {"refresh_token": "rt-2"})

    assert updated.id == "c1"
    assert updated.payload == {"refresh_token": "rt-2"}
    assert updated.updated_at >= before


def test_created_at_preserved_across_update(repo):
    """created_at is fixed at add time; update_payload only bumps
    updated_at (timestamp semantics frozen by the contract)."""
    credential = repo.add(oauth_credential())
    updated = repo.update_payload("c1", {"refresh_token": "rt-2"})

    assert updated.created_at == credential.created_at


def test_update_unknown_raises(repo):
    with pytest.raises(UnknownCredentialError):
        repo.update_payload("missing", {})


def test_remove_then_get_is_none(repo):
    repo.add(oauth_credential())
    repo.remove("c1")
    assert repo.get("c1") is None


def test_remove_unknown_is_idempotent(repo):
    repo.remove("never-existed")  # no raise
    repo.remove("never-existed")  # still no raise


def test_list_returns_insertion_order(repo):
    """Explicit distinct created_at keeps insertion order meaningful for
    every implementation (PostgreSQL orders by created_at, id)."""
    base = Credential(id="seed").created_at
    c1 = oauth_credential("c1")
    c1.created_at = base
    k1 = api_key_credential("k1")
    k1.created_at = base + timedelta(seconds=1)
    c2 = oauth_credential("c2")
    c2.created_at = base + timedelta(seconds=2)
    repo.add(c1)
    repo.add(k1)
    repo.add(c2)
    assert [c.id for c in repo.list()] == ["c1", "k1", "c2"]


def test_existence_check(repo):
    repo.add(oauth_credential())
    assert "c1" in repo
    assert "missing" not in repo


# -- error semantics -------------------------------------------------------------


def test_duplicate_id_rejected(repo):
    repo.add(oauth_credential("c1"))
    with pytest.raises(DuplicateCredentialError):
        repo.add(oauth_credential("c1"))


# -- isolation --------------------------------------------------------------------


def test_two_repositories_are_isolated():
    for make_repo in IMPLEMENTATIONS:
        repo_a, repo_b = make_repo(), make_repo()
        repo_a.add(oauth_credential("c1"))

        assert repo_a.get("c1") is not None
        assert repo_b.get("c1") is None


# -- payload shapes -----------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"refresh_token": "rt", "client_id": "cid", "client_secret": "cs"},
        {"api_key": "AIzaSyX", "app_id": "app", "debug_token": "dbg"},
        {"nested": {"value": 123, "list": [1, 2, {"deep": True}]}},
        {"unicode": "中文🔐±§", "note": "emoji 🚀"},
    ],
    ids=["empty", "oauth", "api-key", "nested", "unicode"],
)
def test_payload_shapes_round_trip(repo, payload):
    credential = repo.add(Credential(id="p1", payload=payload))
    assert repo.get("p1").payload == payload
    assert credential.payload == payload


def test_payload_object_not_shared_with_caller(repo):
    """Mutating the caller's dict after add must not affect the store."""
    payload = {"refresh_token": "rt"}
    credential = repo.add(Credential(id="p1", payload=payload))
    payload["refresh_token"] = "mutated"
    assert credential.payload["refresh_token"] == "rt"


# -- durable-shape boundary -----------------------------------------------------------


def test_repository_stores_only_durable_credential_shape(repo):
    """id / type / payload / timestamps; no runtime auth state keys are
    created by the repository itself."""
    credential = repo.add(oauth_credential())
    data = credential.redacted_dict()
    assert set(data) == {"id", "type", "payload", "created_at", "updated_at"}
    # runtime-only fields never appear on the durable shape
    for runtime_key in (
        "access_token",
        "expires_at",
        "jwt",
        "rotated_refresh_token",
        "in_flight",
    ):
        assert runtime_key not in Credential.model_fields


def test_provider_surface_needs_only_get_and_require(repo):
    """Provider adapters resolve credentials exclusively via get/require —
    the minimal surface the contract must guarantee."""
    repo.add(oauth_credential())
    assert repo.get("c1").type is CredentialType.OAUTH
    assert repo.require("c1").id == "c1"
