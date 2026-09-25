"""PostgreSQL credential repository (TASK-AUTH-009).

The first durable implementation of the AUTH-008
:class:`core.credential.CredentialRepository` contract::

    Credential (domain)
        ↓
    PostgreSQLCredentialRepository      (this module)
        │   encrypt payload on write / decrypt on read
        │   AAD binds credential.id + credential.type
        ▼
    PostgreSQL / Supabase PostgreSQL    (standard DSN; hosting is deployment)

Boundary rules frozen by AUTH-009:

* Provider-agnostic schema: one ``credentials`` table, payload stored as
  the AUTH-007 encrypted envelope.  No provider-specific columns, no
  plaintext secret columns.
* Only ``Credential.payload`` is encrypted at rest; id / type /
  created_at / updated_at are plaintext columns.
* The repository is synchronous (AUTH-008 contract); the driver adapts
  to it — never the reverse.  The repository duck-types the connection
  (``execute``/``commit``/``rollback``/``close`` and cursor
  ``fetchone``/``fetchall``/``rowcount``), so no database SDK import is
  required to use or test it.  :func:`psycopg_connection_factory` is the
  only place that touches psycopg, and it imports lazily.
* Duplicate detection relies on the PostgreSQL PRIMARY KEY constraint
  (SQLSTATE 23505 → DuplicateCredentialError).  Other driver, database
  and connection exceptions are NOT wrapped: they propagate unchanged so
  infrastructure failures remain distinguishable from contract errors.
* A corrupted row (bad envelope, tampered ciphertext, wrong key) raises
  :class:`CredentialDecryptionError` — corruption is never mistaken for
  "credential missing".
* Runtime auth state (access tokens, JWTs, expiry caches, rotated
  refresh tokens, scheduling counters) never enters this repository;
  it persists only the durable Credential shape.
* Startup: when this repository is explicitly configured, a database
  failure propagates — it is never silently replaced by an empty
  in-memory store (infrastructure failure must not masquerade as
  authentication failure).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from core.credential import (
    Credential,
    CredentialRepository,
    CredentialType,
    DuplicateCredentialError,
    UnknownCredentialError,
)
from core.credential_encryption import (
    CredentialDecryptionError,
    CredentialEncryptor,
)

# -- schema (repeatable, provider-agnostic) ---------------------------------------

CREDENTIALS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS credentials (
    id                TEXT PRIMARY KEY,
    type              TEXT NOT NULL,
    payload_encrypted JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL
)
"""

# Repository statements (module constants so test doubles can target them
# without parsing free-form SQL).
_INSERT_CREDENTIAL_SQL = (
    "INSERT INTO credentials (id, type, payload_encrypted, created_at, "
    "updated_at) VALUES (%s, %s, %s, %s, %s)"
)
_SELECT_BY_ID_SQL = (
    "SELECT id, type, payload_encrypted, created_at, updated_at "
    "FROM credentials WHERE id = %s"
)
_SELECT_TYPE_FOR_UPDATE_SQL = (
    "SELECT type FROM credentials WHERE id = %s FOR UPDATE"
)
_UPDATE_PAYLOAD_RETURNING_SQL = (
    "UPDATE credentials SET payload_encrypted = %s, updated_at = %s "
    "WHERE id = %s "
    "RETURNING id, type, payload_encrypted, created_at, updated_at"
)
_DELETE_CREDENTIAL_SQL = "DELETE FROM credentials WHERE id = %s"
_LIST_CREDENTIALS_SQL = (
    "SELECT id, type, payload_encrypted, created_at, updated_at "
    "FROM credentials ORDER BY created_at, id"
)
_LIST_FOR_UPDATE_SQL = _LIST_CREDENTIALS_SQL + " FOR UPDATE"
# Key rotation rewrites ONLY the envelope: id/type/created_at/updated_at
# are untouched (rotation is not a credential update).
_ROTATE_PAYLOAD_SQL = "UPDATE credentials SET payload_encrypted = %s WHERE id = %s"

# PostgreSQL constraint violation for duplicate primary keys (DBAPI
# SQLSTATE; detected without importing any driver).
_UNIQUE_VIOLATION_SQLSTATE = "23505"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def credential_aad(credential_id: str, credential_type: str) -> bytes:
    """Canonical, deterministic AAD binding a payload to its record.

    Binds ``id`` and ``type`` (the record identity stored in plaintext
    columns) so ciphertexts cannot be swapped between records or have
    their type silently rewritten.  Contains no payload secret; UTF-8
    canonical JSON, identical on write and read paths.
    """
    return json.dumps(
        {"id": credential_id, "type": credential_type},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def ensure_credentials_schema(connection: Any) -> None:
    """Create the credentials table if absent (repeatable, non-destructive).

    Raises on database failure — callers treat that as a startup failure.
    """
    connection.execute(CREDENTIALS_SCHEMA_SQL)
    connection.commit()


class PostgreSQLCredentialRepository(CredentialRepository):
    """Durable, encrypted, provider-agnostic credential repository.

    ``connection_factory`` returns a fresh DBAPI-style connection per
    operation (the repository commits on success, rolls back on error and
    always closes).  Works with any driver exposing psycopg-3-style
    ``execute``/cursors — including test doubles.
    """

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        encryptor: CredentialEncryptor,
        clock: Optional[Any] = None,
    ) -> None:
        self._connection_factory = connection_factory
        self._encryptor = encryptor
        self._clock = clock if clock is not None else utcnow

    # -- connection handling -------------------------------------------------

    def _now(self) -> datetime:
        """Current UTC timestamp; ``clock`` is a zero-arg callable
        returning a datetime (defaults to :func:`utcnow`)."""
        return self._clock()

    class _Transaction:
        """One operation = one connection: commit / rollback / close."""

        def __init__(self, factory: Callable[[], Any]) -> None:
            self._factory = factory
            self.connection: Any = None

        def __enter__(self) -> Any:
            self.connection = self._factory()
            return self.connection

        def __exit__(self, exc_type, exc, tb) -> bool:
            try:
                if exc_type is None:
                    self.connection.commit()
                else:
                    self.connection.rollback()
            finally:
                try:
                    self.connection.close()
                except Exception:  # noqa: BLE001 - close must not mask
                    pass
            return False  # never swallow operation errors

    # -- lifecycle -----------------------------------------------------------------

    def initialize(self) -> None:
        """Create the credentials table (repeatable, non-destructive).

        Any database failure propagates to the caller — application
        startup must fail loudly when the durable repository was
        explicitly enabled (never fall back to an empty in-memory store).
        """
        with self._Transaction(self._connection_factory) as conn:
            conn.execute(CREDENTIALS_SCHEMA_SQL)

    # -- AUTH-008 contract -------------------------------------------------------

    def add(self, credential: Credential) -> Credential:
        aad = credential_aad(credential.id, credential.type.value)
        envelope = self._encryptor.encrypt_payload(
            credential.payload, associated_data=aad
        )
        with self._Transaction(self._connection_factory) as conn:
            try:
                conn.execute(
                    _INSERT_CREDENTIAL_SQL,
                    (
                        credential.id,
                        credential.type.value,
                        json.dumps(envelope),
                        credential.created_at,
                        credential.updated_at,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - translated below
                if getattr(exc, "sqlstate", None) == _UNIQUE_VIOLATION_SQLSTATE:
                    raise DuplicateCredentialError(
                        f"credential '{credential.id}' already registered"
                    ) from exc
                raise
        return credential

    def get(self, credential_id: str) -> Optional[Credential]:
        with self._Transaction(self._connection_factory) as conn:
            row = conn.execute(_SELECT_BY_ID_SQL, (credential_id,)).fetchone()
        if row is None:
            return None
        return self._decrypt_row(row)

    def require(self, credential_id: str) -> Credential:
        credential = self.get(credential_id)
        if credential is None:
            raise UnknownCredentialError(
                f"credential '{credential_id}' is not registered"
            )
        return credential

    def update_payload(
        self,
        credential_id: str,
        payload: Dict[str, Any],
    ) -> Credential:
        """Atomically replace a credential's payload.

        Single database transaction: ``SELECT type ... FOR UPDATE`` (row
        lock; supplies the AAD type binding) followed by
        ``UPDATE ... RETURNING``.  Existence is decided inside the same
        transaction by the UPDATE itself — there is no independent
        pre-SELECT, and a concurrent delete cannot slip through (the row
        lock blocks it; a deleted row yields zero rows).

        Failure semantics: unknown id -> UnknownCredentialError (with
        rollback); any driver/database failure -> rollback and the
        original infrastructure exception propagates — it is never
        translated into UnknownCredentialError.
        """
        with self._Transaction(self._connection_factory) as conn:
            type_row = conn.execute(
                _SELECT_TYPE_FOR_UPDATE_SQL, (credential_id,)
            ).fetchone()
            if type_row is None:
                raise UnknownCredentialError(
                    f"credential '{credential_id}' is not registered"
                )
            aad = credential_aad(credential_id, type_row["type"])
            envelope = self._encryptor.encrypt_payload(
                payload, associated_data=aad
            )
            updated_at = self._now()
            cursor = conn.execute(
                _UPDATE_PAYLOAD_RETURNING_SQL,
                (json.dumps(envelope), updated_at, credential_id),
            )
            row = cursor.fetchone()
            if row is None:  # defensive: deleted between lock and update
                raise UnknownCredentialError(
                    f"credential '{credential_id}' is not registered"
                )
        return self._decrypt_row(row)

    def remove(self, credential_id: str) -> None:
        with self._Transaction(self._connection_factory) as conn:
            conn.execute(_DELETE_CREDENTIAL_SQL, (credential_id,))
        # Unknown ids are a no-op (AUTH-008 contract); DELETE affects 0 rows.

    def list(self) -> List[Credential]:
        with self._Transaction(self._connection_factory) as conn:
            rows = conn.execute(_LIST_CREDENTIALS_SQL).fetchall()
        # Deterministic ORDER BY created_at, id (enforced by the SQL);
        # corrupt rows fail closed instead of being skipped.
        return [self._decrypt_row(row) for row in rows]

    # -- key rotation (AUTH-012) -------------------------------------------------

    def rotate_key(self, new_encryptor: CredentialEncryptor) -> Dict[str, int]:
        """Re-encrypt every credential payload under the new encryptor.

        Single transaction: rows are selected ``FOR UPDATE``, each
        envelope whose ``kid`` differs from the new active kid is
        decrypted with the CURRENT repository encryptor (whose keyring
        must still contain that kid) and re-encrypted with
        ``new_encryptor`` under the SAME AAD (id + type) and a fresh
        nonce.  Envelopes already under the active kid are skipped and
        not rewritten.  ``id`` / ``type`` / ``created_at`` /
        ``updated_at`` are never modified.

        Failure semantics: any decryption or driver failure rolls the
        whole batch back (nothing is half-rotated) and the original
        exception propagates; ``self._encryptor`` is only adopted after a
        successful commit.

        Returns ``{"total", "rotated", "skipped"}`` — no payload material.
        """
        rotated = 0
        skipped = 0
        with self._Transaction(self._connection_factory) as conn:
            rows = conn.execute(_LIST_FOR_UPDATE_SQL).fetchall()
            for row in rows:
                envelope = row["payload_encrypted"]
                if envelope.get("kid") == new_encryptor.active_kid:
                    skipped += 1
                    continue
                aad = credential_aad(row["id"], row["type"])
                payload = self._encryptor.decrypt_payload(
                    envelope, associated_data=aad
                )
                new_envelope = new_encryptor.encrypt_payload(
                    payload, associated_data=aad
                )
                cursor = conn.execute(
                    _ROTATE_PAYLOAD_SQL,
                    (json.dumps(new_envelope), row["id"]),
                )
                if getattr(cursor, "rowcount", 1) == 0:
                    raise CredentialDecryptionError(
                        "key rotation lost its locked row; rolled back"
                    )
                rotated += 1
        self._encryptor = new_encryptor
        return {"total": len(rows), "rotated": rotated, "skipped": skipped}

    # -- encryption helpers --------------------------------------------------------

    def _decrypt_row(self, row: Any) -> Credential:
        aad = credential_aad(row["id"], row["type"])
        payload = self._encryptor.decrypt_payload(
            row["payload_encrypted"], associated_data=aad
        )
        return Credential(
            id=row["id"],
            type=CredentialType(row["type"]),
            payload=payload,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def psycopg_connection_factory(dsn: str) -> Callable[[], Any]:
    """Build a connection factory over psycopg 3 (lazy import).

    The returned factory opens a fresh connection per call with dict rows,
    matching the repository's duck-typed connection surface.  Import and
    connection errors propagate to the caller (startup failure semantics).
    """
    def factory() -> Any:
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(dsn, row_factory=dict_row)

    return factory
