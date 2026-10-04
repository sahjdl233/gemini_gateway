"""Persistence-layer schema migration (CONFIG/R-6 Part 1).

Standard PostgreSQL only — no vendor extensions, no sqlite fallback, no
local-file dependencies.  Idempotent: every statement is safe to re-run
(CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS), so any adapter
can call :func:`initialize_persistence` at startup.

Tables:

* ``resource_definitions`` — upgraded in place.  The table already
  exists (created by ``core.resource_postgres.initialize``); the
  migration adds the persistence-layer columns (surrogate ``id``,
  ``created_at`` / ``updated_at``) without touching the frozen composite
  identity ``PRIMARY KEY (provider, resource_id)``.  There is
  deliberately NO standalone unique on ``resource_id`` — identity is
  provider + id (ADR-CONFIG-R4).
* ``credentials`` — owned by the AUTH-009 credential repository; the
  migration only ensures the table exists (same DDL, one source of
  truth).  Encryption design is untouched.
* ``runtime_state`` — new table for discardable scheduling state.
  Nullable / absent rows are normal: startup never depends on this
  table having content.  ``latency_stats`` is a reserved JSONB column
  (unused by the v1 adapter).
"""

from __future__ import annotations

from typing import Any, Callable, List

from core.credential_postgres import CREDENTIALS_SCHEMA_SQL

__all__ = [
    "PERSISTENCE_SCHEMA_STATEMENTS",
    "initialize_persistence",
]

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

#: In-place upgrade of a pre-R-6 table: additive only, so the existing
#: ``PostgreSQLResourceRepository`` startup path keeps working unchanged.
RESOURCE_DEFINITIONS_UPGRADE_SQL = (
    "ALTER TABLE resource_definitions "
    "ADD COLUMN IF NOT EXISTS id BIGSERIAL",
    "ALTER TABLE resource_definitions "
    "ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ "
    "NOT NULL DEFAULT now()",
    "ALTER TABLE resource_definitions "
    "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ "
    "NOT NULL DEFAULT now()",
)

RUNTIME_STATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runtime_state (
    provider             TEXT NOT NULL,
    resource_id          TEXT NOT NULL,
    health               TEXT NOT NULL,
    cooldown_until       TIMESTAMPTZ NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    total_requests       INTEGER NOT NULL DEFAULT 0,
    total_failures       INTEGER NOT NULL DEFAULT 0,
    latency_stats        JSONB NULL,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, resource_id)
)
"""

PERSISTENCE_SCHEMA_STATEMENTS: List[str] = [
    RESOURCE_DEFINITIONS_SCHEMA_SQL,
    *RESOURCE_DEFINITIONS_UPGRADE_SQL,
    CREDENTIALS_SCHEMA_SQL,
    RUNTIME_STATE_SCHEMA_SQL,
]


async def initialize_persistence(connection_factory: Callable[[], Any]) -> None:
    """Run the idempotent schema migration over one connection.

    Commit on success; rollback + close on any error, which propagates
    unchanged (startup must fail loudly when persistence was requested).
    """
    connection = await connection_factory()
    try:
        for statement in PERSISTENCE_SCHEMA_STATEMENTS:
            await connection.execute(statement)
        await connection.commit()
    except BaseException:
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
