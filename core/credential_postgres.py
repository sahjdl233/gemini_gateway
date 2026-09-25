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
  (SQLSTATE 23505 → DuplicateCredentialError); driver exceptions are
  never leaked through the contract.
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
_UPDATE_PAYLOAD_SQL = (
    "UPDATE credentials SET payload_encrypted = %s, updated_at = %s "
    "WHERE id = %s"
)
_DELETE_CREDENTIAL_SQL = "DELETE FROM credentials WHERE id = %s"
_LIST_CREDENTIALS_SQL = (
    "SELECT id, type, payload_encrypted, created_at, updated_at "
    "FROM credentials ORDER BY created_at, id"
)

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
        row = self.get(credential_id)  # decrypt check + existence check
        if row is None:
            raise UnknownCredentialError(
                f"credential '{credential_id}' is not registered"
            )
        aad = credential_aad(credential_id, row.type.value)
        envelope = self._encryptor.encrypt_payload(
            payload, associated_data=aad
        )
        updated_at = self._now()
        with self._Transaction(self._connection_factory) as conn:
            cursor = conn.execute(
                _UPDATE_PAYLOAD_SQL,
                (json.dumps(envelope), updated_at, credential_id),
            )
            if getattr(cursor, "rowcount", 1) == 0:
                raise UnknownCredentialError(
                    f"credential '{credential_id}' is not registered"
                )
        row.payload = dict(payload)
        row.updated_at = updated_at
        return row

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
