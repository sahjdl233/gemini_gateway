"""PostgreSQL resource-definition repository (DB-RESOURCE-001-3/4).

Persistence layer for :class:`core.resource_definition.ResourceDefinitionBase`
DTOs, mirroring the pattern of :mod:`core.credential_postgres`
(TASK-AUTH-009) but on the **asynchronous**
:class:`core.resource_repository.ResourceRepository` contract:

* Composite identity ``(provider, resource_id)`` — the table's PRIMARY
  KEY.  ``resource_id`` gets NO standalone UNIQUE constraint.
* No foreign key to ``credentials``: the reference is loose (stored as
  ``credential_id``), resolved — or warned about — by callers.
* Only the durable definition shape is stored (``definition`` JSONB holds
  the provider-specific non-secret body from
  :meth:`ResourceDefinitionBase.to_definition_json`).  Runtime ``Resource``
  state, provider clients, health counters, tokens and secrets never
  enter this table.
* No schema version table, no extra indexes at this stage.

Boundary rules:

* ``initialize()`` is repeatable and non-destructive
  (``CREATE TABLE IF NOT EXISTS``); success commits, any driver or
  database error propagates unchanged — never swallowed, never
  silently replaced by an in-memory fallback.
* The repository duck-types an *async* connection (:class:`AsyncConnectionProtocol`:
  ``await execute`` / ``commit`` / ``rollback`` / ``close`` with cursor
  ``await fetchone`` / ``fetchall`` / ``rowcount``), so no driver import
  is required to use or test it.
  :func:`psycopg_async_connection_factory` is the only place that
  touches psycopg, and it imports lazily.
* One operation = one connection: commit on success, rollback on error,
  close on both paths; a ``close`` failure never masks the original
  error.
* Duplicate detection relies on the PostgreSQL PRIMARY KEY constraint
  (SQLSTATE 23505 → DuplicateResourceDefinitionError).  Other driver,
  database and connection exceptions are NOT wrapped: they propagate
  unchanged.  psycopg ``IntegrityError`` is never exposed directly.
* ``update`` is a full replacement decided by the UPDATE itself
  (``rowcount == 0`` → UnknownResourceDefinitionError); it never falls
  back to an upsert.
* ``delete`` is idempotent: deleting an unknown key is a no-op.

Design baseline: ``docs/DB-RESOURCE-DESIGN-001.md`` §2.3, §4, §6, §8.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, List, Optional, Protocol

from core.resource_definition import (
    ResourceDefinitionBase,
    resource_definition_from_row,
)
from core.resource_repository import (
    DuplicateResourceDefinitionError,
    ResourceRepository,
    UnknownResourceDefinitionError,
)

# -- schema (repeatable, provider-agnostic) ---------------------------------------

RESOURCE_DEFINITIONS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS resource_definitions (
    provider      TEXT NOT NULL,
    resource_id   TEXT NOT NULL,
    enabled       BOOLEAN NOT NULL,
    credential_id TEXT NULL,
    definition    JSONB NOT NULL,
    PRIMARY KEY (provider, resource_id)
)
"""

# Repository statements (module constants so test doubles can target them
# without parsing free-form SQL).
_INSERT_DEFINITION_SQL = (
    "INSERT INTO resource_definitions (provider, resource_id, enabled, "
    "credential_id, definition) VALUES (%s, %s, %s, %s, %s)"
)
_SELECT_BY_KEY_SQL = (
    "SELECT provider, resource_id, enabled, credential_id, definition "
    "FROM resource_definitions WHERE provider = %s AND resource_id = %s"
)
_LIST_ALL_SQL = (
    "SELECT provider, resource_id, enabled, credential_id, definition "
    "FROM resource_definitions ORDER BY provider, resource_id"
)
_LIST_BY_PROVIDER_SQL = (
    "SELECT provider, resource_id, enabled, credential_id, definition "
    "FROM resource_definitions WHERE provider = %s ORDER BY resource_id"
)
_UPDATE_DEFINITION_SQL = (
    "UPDATE resource_definitions SET enabled = %s, credential_id = %s, "
    "definition = %s WHERE provider = %s AND resource_id = %s"
)
_DELETE_DEFINITION_SQL = (
    "DELETE FROM resource_definitions WHERE provider = %s AND resource_id = %s"
)

# PostgreSQL constraint violation for duplicate primary keys (DBAPI
# SQLSTATE; detected without importing any driver).
_UNIQUE_VIOLATION_SQLSTATE = "23505"


class AsyncCursorProtocol(Protocol):
    """Cursor surface the repository duck-types against.

    Mirrors psycopg 3 ``AsyncCursor``: ``fetchone`` / ``fetchall`` are
    awaitable; ``rowcount`` is a plain attribute.  Purely a typing aid —
    no runtime isinstance checks, any object with this surface works.
    """

    rowcount: int

    async def fetchone(self) -> Optional[Any]: ...

    async def fetchall(self) -> List[Any]: ...


class AsyncConnectionProtocol(Protocol):
    """Structural surface the repository duck-types against.

    Matches psycopg 3 ``AsyncConnection`` (dict-row cursors) closely
    enough for the repository and its test doubles.  Purely a typing
    aid — connections are never checked via isinstance, so any object
    exposing this async surface works.
    """

    async def execute(
        self, sql: str, params: Optional[tuple] = None
    ) -> AsyncCursorProtocol: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def close(self) -> None: ...


class PostgreSQLResourceRepository(ResourceRepository):
    """Durable, provider-agnostic resource-definition repository.

    ``connection_factory`` is a zero-arg **async** callable returning a
    fresh connection per operation (the repository commits on success,
    rolls back on error and always closes).  Works with any driver
    exposing psycopg-3-style async ``execute``/cursors — including test
    doubles.
    """

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connection_factory = connection_factory

    # -- connection handling -------------------------------------------------

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[Any]:
        """One operation = one connection: commit / rollback / close.

        Errors from the operation body roll the transaction back and
        propagate unchanged; close and rollback failures never mask the
        original exception.
        """
        connection = await self._connection_factory()
        try:
            yield connection
            await connection.commit()
        except BaseException:
            # BaseException, not Exception: asyncio cancellation delivers
            # CancelledError (a BaseException) — the connection must still
            # be rolled back and closed, or a cancelled request leaks its
            # transaction.  Original exception always propagates.
            try:
                await connection.rollback()
            except Exception:  # noqa: BLE001 - rollback must not mask
                pass
            raise
        finally:
            try:
                await connection.close()
            except Exception:  # noqa: BLE001 - close must not mask
                pass

    # -- row mapping ---------------------------------------------------------

    @staticmethod
    def _row_to_definition(row: Any) -> ResourceDefinitionBase:
        """Rebuild the strict DTO from identity columns + JSONB body."""
        return resource_definition_from_row(
            provider=row["provider"],
            resource_id=row["resource_id"],
            enabled=row["enabled"],
            credential_id=row["credential_id"],
            definition=row["definition"],
        )

    # -- lifecycle (DB-RESOURCE-001-3) --------------------------------------------

    async def initialize(self) -> None:
        """Create the ``resource_definitions`` table (repeatable,
        non-destructive).

        Any driver or database failure propagates to the caller —
        application startup must fail loudly when the durable repository
        was explicitly enabled (never fall back to an in-memory store).
        """
        async with self._transaction() as connection:
            await connection.execute(RESOURCE_DEFINITIONS_SCHEMA_SQL)

    # -- ResourceRepository contract (DB-RESOURCE-001-4) ---------------------------

    async def add(
        self, definition: ResourceDefinitionBase
    ) -> ResourceDefinitionBase:
        body = definition.to_definition_json()
        async with self._transaction() as connection:
            try:
                await connection.execute(
                    _INSERT_DEFINITION_SQL,
                    (
                        definition.provider,
                        definition.id,
                        definition.enabled,
                        definition.credential_id,
                        json.dumps(body),
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - translated below
                if getattr(exc, "sqlstate", None) == _UNIQUE_VIOLATION_SQLSTATE:
                    raise DuplicateResourceDefinitionError(
                        definition.provider,
                        definition.id,
                        f"resource definition already exists: "
                        f"provider={definition.provider!r}, "
                        f"id={definition.id!r}",
                    ) from exc
                raise
        return definition

    async def get(
        self, provider: str, resource_id: str
    ) -> Optional[ResourceDefinitionBase]:
        async with self._transaction() as connection:
            cursor = await connection.execute(
                _SELECT_BY_KEY_SQL, (provider, resource_id)
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_definition(row)

    async def require(
        self, provider: str, resource_id: str
    ) -> ResourceDefinitionBase:
        definition = await self.get(provider, resource_id)
        if definition is None:
            raise UnknownResourceDefinitionError(
                provider,
                resource_id,
                f"resource definition not found: provider={provider!r}, "
                f"id={resource_id!r}",
            )
        return definition

    async def list(
        self, *, provider: Optional[str] = None
    ) -> List[ResourceDefinitionBase]:
        if provider is not None:
            sql: str = _LIST_BY_PROVIDER_SQL
            params: tuple = (provider,)
        else:
            sql = _LIST_ALL_SQL
            params = ()
        async with self._transaction() as connection:
            cursor = await connection.execute(sql, params)
            rows = await cursor.fetchall()
        # Deterministic ORDER BY is enforced by the SQL (composite key for
        # the unfiltered listing, resource_id within a provider).
        return [self._row_to_definition(row) for row in rows]

    async def update(
        self, definition: ResourceDefinitionBase
    ) -> ResourceDefinitionBase:
        """Fully replace the definition for the composite key.

        A single UPDATE decides existence via ``rowcount`` — there is no
        pre-SELECT and no upsert fallback (the contract forbids update
        becoming insert).  Unknown key raises
        UnknownResourceDefinitionError with rollback.
        """
        body = definition.to_definition_json()
        async with self._transaction() as connection:
            cursor = await connection.execute(
                _UPDATE_DEFINITION_SQL,
                (
                    definition.enabled,
                    definition.credential_id,
                    json.dumps(body),
                    definition.provider,
                    definition.id,
                ),
            )
            if getattr(cursor, "rowcount", 0) == 0:
                raise UnknownResourceDefinitionError(
                    definition.provider,
                    definition.id,
                    f"resource definition not found: "
                    f"provider={definition.provider!r}, "
                    f"id={definition.id!r}",
                )
        return definition

    async def delete(self, provider: str, resource_id: str) -> None:
        async with self._transaction() as connection:
            # Unknown keys are a no-op (contract: delete is idempotent);
            # DELETE affects 0 rows and must not raise.
            await connection.execute(
                _DELETE_DEFINITION_SQL, (provider, resource_id)
            )


def psycopg_async_connection_factory(dsn: str) -> Callable[[], Any]:
    """Build an async connection factory over psycopg 3 (lazy import).

    The returned factory must be awaited and opens a fresh
    :class:`psycopg.AsyncConnection` per call with dict rows, matching
    the repository's async duck-typed connection surface.  Import and
    connection errors propagate to the caller (startup failure
    semantics).
    """
    async def factory() -> Any:
        import psycopg
        from psycopg.rows import dict_row

        return await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row)

    return factory
