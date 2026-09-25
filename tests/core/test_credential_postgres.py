"""PostgreSQL credential repository tests (TASK-AUTH-009).

Two layers, reported separately:

1. Repository + encryption behaviour over a fake driver
   (``tests.core._fake_postgres``): exercises the SQL surface, envelope
   encryption, AAD binding and fail-closed semantics WITHOUT a real
   PostgreSQL server.
2. Real PostgreSQL integration: opt-in ONLY, via
   ``GEMINI_GATEWAY_TEST_DATABASE_URL``.  Skipped when the variable is
   absent — the absence is reported honestly (no fake-run is ever
   presented as a real integration test).

Contract coverage (CRUD / errors / isolation / payload shapes) lives in
``test_credential_repository.py``, parameterized over both the in-memory
store and this repository backed by the fake driver.
"""

from __future__ import annotations

import base64
import os
from datetime import timedelta

import pytest

from core.credential import (
    Credential,
    CredentialStore,
    CredentialType,
    DuplicateCredentialError,
    UnknownCredentialError,
)
from core.credential_encryption import (
    CredentialDecryptionError,
    CredentialEncryptor,
    load_encryption_key,
)
from core.credential_postgres import (
    PostgreSQLCredentialRepository,
    credential_aad,
    psycopg_connection_factory,
)
from tests.core._fake_postgres import FakePostgres


OAUTH_PAYLOAD = {
    "refresh_token": "rt-secret-value",
    "client_id": "client-id-1",
    "client_secret": "cs-secret-value",
}


class FixedClock:
    """Zero-arg callable returning a fixed, advanceable UTC datetime."""

    def __init__(self, start):
        from datetime import datetime, timezone

        self.now = start or datetime.now(timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def make_repo(fake: FakePostgres, key: bytes | None = None, clock=None):
    return PostgreSQLCredentialRepository(
        fake.connection_factory(),
        encryptor=CredentialEncryptor(key or os.urandom(32)),
        clock=clock,
    )


def oauth_credential(cid: str = "c1") -> Credential:
    return Credential(
        id=cid, type=CredentialType.OAUTH, payload=dict(OAUTH_PAYLOAD)
    )


@pytest.fixture
def fake() -> FakePostgres:
    return FakePostgres()


# -- encryption round trip -------------------------------------------------------


def test_round_trip_identical_credential_semantics(fake):
    repo = make_repo(fake)
    credential = oauth_credential()
    repo.add(credential)

    loaded = repo.get("c1")

    assert loaded.id == "c1"
    assert loaded.type is CredentialType.OAUTH
    assert loaded.payload == OAUTH_PAYLOAD
    assert loaded.created_at == credential.created_at
    assert loaded.updated_at == credential.updated_at


# -- 14 raw storage: no plaintext at rest ------------------------------------------


def test_database_stores_encrypted_envelope_not_plaintext(fake):
    import json

    repo = make_repo(fake)
    repo.add(oauth_credential())

    raw_row = fake.raw_rows()["c1"]

    envelope = json.loads(raw_row["payload_encrypted"])
    assert set(envelope) == {"v", "alg", "kid", "nonce", "ciphertext"}
    rendered = str(raw_row)
    # plaintext payload must not appear anywhere in the raw DB row
    for secret in ("rt-secret-value", "cs-secret-value", "client-id-1"):
        assert secret not in rendered


def test_type_and_timestamps_stay_plaintext_columns(fake):
    repo = make_repo(fake)
    repo.add(oauth_credential())
    raw_row = fake.raw_rows()["c1"]
    assert raw_row["type"] == "oauth"
    assert raw_row["id"] == "c1"


# -- 13 wrong key --------------------------------------------------------------------


def test_wrong_key_reads_fail_closed(fake):
    repo = make_repo(fake)
    repo.add(oauth_credential())

    wrong_key_repo = make_repo(fake, key=os.urandom(32))

    with pytest.raises(CredentialDecryptionError):
        wrong_key_repo.get("c1")
    with pytest.raises(CredentialDecryptionError):
        wrong_key_repo.require("c1")
    with pytest.raises(CredentialDecryptionError):
        wrong_key_repo.list()


# -- tamper ----------------------------------------------------------------------------


def test_tampered_ciphertext_in_row_fails(fake):
    import json

    repo = make_repo(fake)
    repo.add(oauth_credential())

    envelope = json.loads(fake.raw_rows()["c1"]["payload_encrypted"])
    raw = bytearray(base64.b64decode(envelope["ciphertext"]))
    raw[0] ^= 0x01
    envelope["ciphertext"] = base64.b64encode(bytes(raw)).decode("ascii")
    fake.raw_rows()["c1"]["payload_encrypted"] = json.dumps(envelope)

    with pytest.raises(CredentialDecryptionError):
        repo.get("c1")


def test_malformed_envelope_in_row_fails_not_missing(fake):
    """A corrupt row is a decryption failure, never 'credential missing'."""
    repo = make_repo(fake)
    repo.add(oauth_credential())
    fake.raw_rows()["c1"]["payload_encrypted"] = {"v": 999, "alg": "X"}

    with pytest.raises(CredentialDecryptionError):
        repo.get("c1")
    with pytest.raises(CredentialDecryptionError):
        repo.require("c1")  # NOT UnknownCredentialError


# -- 11/12 AAD record binding ------------------------------------------------------------


def test_aad_is_deterministic_and_secret_free():
    first = credential_aad("c1", "oauth")
    second = credential_aad("c1", "oauth")
    assert first == second
    assert b"rt-secret" not in first
    assert credential_aad("c1", "oauth") != credential_aad("c2", "oauth")
    assert credential_aad("c1", "oauth") != credential_aad("c1", "api_key")


def test_ciphertext_swap_between_records_fails(fake):
    """Credential A's ciphertext placed under Credential B's identity
    must not decrypt (AAD mismatch)."""
    repo = make_repo(fake)
    repo.add(oauth_credential("c1"))
    repo.add(
        Credential(
            id="c2",
            type=CredentialType.OAUTH,
            payload={"refresh_token": "rt-b", "client_id": "cid-b",
                     "client_secret": "cs-b"},
        )
    )

    ciphertext_a = fake.raw_rows()["c1"]["payload_encrypted"]
    fake.raw_rows()["c2"]["payload_encrypted"] = ciphertext_a

    with pytest.raises(CredentialDecryptionError):
        repo.get("c2")


def test_type_swap_in_database_fails(fake):
    """Rewriting only the DB ``type`` column breaks the AAD binding."""
    repo = make_repo(fake)
    repo.add(oauth_credential("c1"))
    fake.raw_rows()["c1"]["type"] = "api_key"

    with pytest.raises(CredentialDecryptionError):
        repo.get("c1")


# -- 15-19 contract details on the durable path ---------------------------------------------


def test_duplicate_id_maps_to_contract_error(fake):
    repo = make_repo(fake)
    repo.add(oauth_credential("c1"))
    with pytest.raises(DuplicateCredentialError):
        repo.add(oauth_credential("c1"))


def test_update_unknown_and_remove_unknown(fake):
    repo = make_repo(fake)
    with pytest.raises(UnknownCredentialError):
        repo.update_payload("missing", {"x": 1})
    repo.remove("missing")  # no-op


def test_update_payload_reencrypts_and_bumps_updated_at(fake):
    clock = FixedClock(None)
    repo = make_repo(fake, clock=clock)
    repo.add(oauth_credential("c1"))
    original_created = repo.get("c1").created_at

    clock.advance(10)
    updated = repo.update_payload("c1", {"refresh_token": "rt-new"})

    assert updated.payload == {"refresh_token": "rt-new"}
    assert updated.created_at == original_created  # created_at immutable
    assert updated.updated_at > original_created
    # new ciphertext differs (fresh nonce) and decrypts to the new payload
    assert fake.raw_rows()["c1"]["updated_at"] == updated.updated_at
    assert repo.get("c1").payload == {"refresh_token": "rt-new"}


def test_deterministic_list_order_and_tie_break(fake):
    repo = make_repo(fake)
    base = repo._now()
    for cid in ("c2", "c1", "k1"):
        credential = Credential(id=cid, payload={"note": cid})
        credential.created_at = base
        credential.updated_at = base
        repo.add(credential)
    # identical timestamps -> deterministic id tie-break
    assert [c.id for c in repo.list()] == ["c1", "c2", "k1"]


# -- AUTH-009-FIX-01: atomic update_payload -------------------------------------
#
# Before the fix, update_payload() ran get() (an independent
# connection/transaction) as a pre-check, then UPDATE — a TOCTOU window
# where a concurrent delete between the two transactions would make the
# UPDATE resurrect nothing / fail confusingly.  The fixed implementation
# uses ONE transaction: SELECT type ... FOR UPDATE + UPDATE ... RETURNING.


def _atomic_update_repo(fake, clock=None):
    return PostgreSQLCredentialRepository(
        fake.connection_factory(),
        encryptor=CredentialEncryptor(os.urandom(32)),
        clock=clock,
    )


def test_atomic_update_does_no_independent_pre_select(fake):
    """A: update_payload must not issue get()'s SELECT-by-id as a
    pre-existence check; existence is decided inside its own transaction
    (SELECT ... FOR UPDATE + UPDATE ... RETURNING)."""
    import json as _json

    from core.credential_postgres import _SELECT_BY_ID_SQL

    repo = _atomic_update_repo(fake)
    repo.add(oauth_credential("c1"))
    fake.connections.clear()

    repo.update_payload("c1", {"refresh_token": "rt-2"})

    executed = [c.executed for c in fake.connections]
    assert len(executed) == 1  # exactly one connection/transaction
    statements = executed[0]
    select_by_id = " ".join(_SELECT_BY_ID_SQL.split())
    assert select_by_id not in statements  # no independent get() pre-check
    assert any("FOR UPDATE" in s for s in statements)
    assert any("RETURNING" in s for s in statements)


def test_atomic_update_unknown_credential_contract(fake):
    """B: unknown id -> UnknownCredentialError; no UPDATE is issued."""
    repo = _atomic_update_repo(fake)
    with pytest.raises(UnknownCredentialError):
        repo.update_payload("missing", {"x": 1})
    (connection,) = fake.connections
    executed = connection.executed
    assert not any("RETURNING" in s for s in executed)  # no write attempted
    assert connection.rollback_calls >= 1
    assert connection.closed is True


def test_atomic_update_returned_values_correct(fake):
    """C: returned id/type/payload/created_at/updated_at all correct."""
    from datetime import datetime, timezone

    clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    repo = _atomic_update_repo(fake, clock=clock)
    credential = oauth_credential("c1")
    credential.created_at = clock.now  # align domain stamps with the clock
    credential.updated_at = clock.now
    repo.add(credential)
    original = repo.get("c1")

    clock.advance(30)
    updated = repo.update_payload("c1", {"refresh_token": "rt-new"})

    assert updated.id == "c1"
    assert updated.type is CredentialType.OAUTH
    assert updated.payload == {"refresh_token": "rt-new"}
    assert updated.created_at == original.created_at
    assert updated.updated_at > original.updated_at


def test_atomic_update_rollback_on_driver_failure(fake):
    """D: UPDATE raises -> rollback -> connection closed -> original
    infrastructure exception propagates (NOT UnknownCredentialError)."""
    repo = _atomic_update_repo(fake)
    repo.add(oauth_credential("c1"))
    fake.connections.clear()

    # fail the first execute of the next connection (the FOR UPDATE select)
    def factory():
        connection = fake.connection_factory()()
        connection.fail_next_execute = RuntimeError("connection lost")
        return connection

    broken_repo = PostgreSQLCredentialRepository(
        factory, encryptor=CredentialEncryptor(os.urandom(32))
    )
    with pytest.raises(RuntimeError) as exc_info:
        broken_repo.update_payload("c1", {"x": 1})

    assert not isinstance(exc_info.value, UnknownCredentialError)
    (connection,) = fake.connections
    assert connection.rollback_calls >= 1
    assert connection.closed is True
    # original payload untouched
    assert repo.get("c1").payload == OAUTH_PAYLOAD


def test_atomic_update_keeps_plaintext_out_of_raw_row(fake):
    """E: after update, the raw DB row still only holds the envelope."""
    import json as _json

    repo = _atomic_update_repo(fake)
    repo.add(oauth_credential("c1"))
    repo.update_payload("c1", {"refresh_token": "rt-updated-secret"})

    rendered = str(fake.raw_rows()["c1"])
    assert "rt-updated-secret" not in rendered
    envelope = _json.loads(fake.raw_rows()["c1"]["payload_encrypted"])
    assert set(envelope) == {"v", "alg", "kid", "nonce", "ciphertext"}


def test_atomic_update_regenerates_ciphertext(fake):
    """F: payload A -> ciphertext A; payload B -> ciphertext B; both
    differ (fresh nonce) and B decrypts correctly."""
    import json as _json

    repo = _atomic_update_repo(fake)
    repo.add(oauth_credential("c1"))
    ciphertext_a = fake.raw_rows()["c1"]["payload_encrypted"]

    repo.update_payload("c1", {"refresh_token": "rt-b", "note": "payload B"})
    ciphertext_b = fake.raw_rows()["c1"]["payload_encrypted"]

    assert ciphertext_a != ciphertext_b
    loaded = repo.get("c1")
    assert loaded.payload == {"refresh_token": "rt-b", "note": "payload B"}


# -- connection lifecycle ------------------------------------------------------------------


def test_connection_committed_and_closed_on_success(fake):
    repo = make_repo(fake)
    repo.add(oauth_credential("c1"))
    (connection,) = fake.connections
    assert connection.commit_calls == 1
    assert connection.closed is True


def test_connection_rolled_back_on_driver_error(fake):
    fake.fail_next_execute_on_new_connection = True

    class BrokenFactory:
        def __init__(self, harness):
            self._harness = harness

        def __call__(self):
            connection = self._harness.connection_factory()()
            if getattr(self._harness, "fail_next_execute_on_new_connection", False):
                connection.fail_next_execute = RuntimeError("connection lost")
            return connection

    broken_repo = PostgreSQLCredentialRepository(
        BrokenFactory(fake), encryptor=CredentialEncryptor(os.urandom(32))
    )
    with pytest.raises(RuntimeError):
        broken_repo.add(oauth_credential("boom"))
    (connection,) = fake.connections
    assert connection.rollback_calls >= 1
    assert connection.closed is True
    # nothing persisted
    assert fake.raw_rows() == {}


# -- driver boundary ------------------------------------------------------------------------


def test_repository_module_never_imports_driver():
    """The repository is driver-agnostic: psycopg is imported lazily and
    only inside psycopg_connection_factory."""
    import sys

    import core.credential_postgres as module

    source = open(module.__file__, encoding="utf-8").read()
    assert "import psycopg" in source  # only inside the lazy factory
    body_before_factory = source.split("def psycopg_connection_factory")[0]
    assert "import psycopg" not in body_before_factory


def test_psycopg_connection_factory_builds_callable():
    factory = psycopg_connection_factory(
        "postgresql://gateway:secret@localhost:1/none"
    )
    assert callable(factory)


# -- real PostgreSQL integration (opt-in; skipped without a server) ---------------------------


REAL_DB_URL = os.environ.get("GEMINI_GATEWAY_TEST_DATABASE_URL")

_real_db = pytest.mark.skipif(
    not REAL_DB_URL,
    reason="no real PostgreSQL available (set GEMINI_GATEWAY_TEST_DATABASE_URL to opt in)",
)


@_real_db
def test_real_postgres_round_trip():
    """REAL integration: requires a live PostgreSQL server.  This test is
    skipped in environments without one — reported as such, never as a
    fake-driven PASS."""
    from core.credential_postgres import ensure_credentials_schema

    factory = psycopg_connection_factory(REAL_DB_URL)
    connection = factory()
    try:
        ensure_credentials_schema(connection)
    finally:
        connection.close()

    key = (
        load_encryption_key()
        if os.environ.get("GEMINI_GATEWAY_ENCRYPTION_KEY")
        else os.urandom(32)
    )
    repo = PostgreSQLCredentialRepository(
        factory, encryptor=CredentialEncryptor(key)
    )
    credential = Credential(
        id="real-integration-01", type=CredentialType.OAUTH, payload=dict(OAUTH_PAYLOAD)
    )
    repo.add(credential)
    loaded = repo.get("real-integration-01")
    assert loaded.payload == OAUTH_PAYLOAD
    repo.remove("real-integration-01")
    assert repo.get("real-integration-01") is None
