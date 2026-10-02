"""PostgreSQL resource-definition repository — schema + initialize
(DB-RESOURCE-001-3).

Persistence layer for :class:`core.resource_definition.ResourceDefinitionBase`
DTOs, mirroring the pattern of :mod:`core.credential_postgres`
(TASK-AUTH-009) but on the **asynchronous**
:class:`core.resource_repository.ResourceRepository` contract:

* Composite identity ``(provider, resource_id)`` — the table's PRIMARY
  KEY.  ``resource_id`` gets NO standalone UNIQUE constraint.
* No foreign key to ``credentials``: the reference is loose (stored as
  ``credential_id``), resolved — or warned about — by callers.
* Only durable definition shape is stored (``definition`` JSONB holds
  the validated non-secret provider fields).  Runtime ``Resource``
  state and secrets never enter this table.
* No schema version table, no extra indexes at this stage.

Boundary rules for this slice (DB-RESOURCE-001-3):

* Only the schema and ``initialize()`` live here; CRUD arrives in a
  follow-up task and must satisfy the async contract tests.
* ``initialize()`` is repeatable and non-destructive
  (``CREATE TABLE IF NOT EXISTS``); success commits, any driver or
  database error propagates unchanged — never swallowed, never
  silently replaced by an in-memory fallback.
* The repository duck-types an *async* connection (``await execute``
  / ``commit`` / ``rollback`` / ``close``), so no driver import is
  required to use or test it.  :func:`psycopg_async_connection_factory`
  is the only place that touches psycopg, and it imports lazily.
* No synchronous DB API is ever called from async methods — the sync
  psycopg factory in :mod:`core.credential_postgres` must not be used
  here.

Design baseline: ``docs/DB-RESOURCE-DESIGN-001.md`` §2.3, §6, §8.
"""

from __future__ import annotations

from typing import Any, Callable

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


class PostgreSQLResourceRepository:
    """Durable, provider-agnostic resource-definition repository
    (PostgreSQL, async).

    ``connection_factory`` is a zero-arg **async** callable returning a
    fresh connection per operation.  This slice provides only
    :meth:`initialize`; the CRUD contract methods are implemented in the
    follow-up task against the same async connection surface.
    """

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connection_factory = connection_factory

    # -- lifecycle ---------------------------------------------------------------

    async def initialize(self) -> None:
        """Create the ``resource_definitions`` table (repeatable,
        non-destructive).

        Opens one connection, executes the schema and commits.  Any
        driver or database failure rolls the transaction back and
        propagates unchanged — application startup must fail loudly when
        the durable repository was explicitly enabled (never fall back
        to an in-memory store).  The connection is closed on both the
        success and the failure path; a ``close`` failure never masks
        the original error.
        """
        connection = await self._connection_factory()
        try:
            await connection.execute(RESOURCE_DEFINITIONS_SCHEMA_SQL)
            await connection.commit()
        except Exception:
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
