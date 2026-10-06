"""ANON-012 Part B E2E: PostgreSQL clean-start persistence migration.

Opt-in, REAL PostgreSQL (``GEMINI_GATEWAY_TEST_DATABASE_URL``); skipped
cleanly without it.

The production startup gap this freezes: with
``resource_store.backend: postgres``, ``create_app`` must complete the
FULL idempotent R6 migration — ``resource_definitions`` upgraded with
``id`` / ``created_at`` / ``updated_at``, plus the ``credentials`` and
``runtime_state`` tables — before/at startup, even on a pristine
database with NO pre-created schema and regardless of whether resource
bootstrap is enabled.  The legacy sink's own ``initialize()`` only
creates the base table and can no longer be relied on for this.

Isolation follows the dbresource012 pattern: a dedicated database
``<dbname>_anon012_clean_start``, tables dropped first so the run is a
genuine clean start (no fixture-initialized schema masking the gap).
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.main import create_app

REAL_DSN = os.environ.get("GEMINI_GATEWAY_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not REAL_DSN,
    reason=(
        "no real PostgreSQL server available; set GEMINI_GATEWAY_TEST_DATABASE_URL "
        "to opt in (this run is reported as an environment limitation, not a PASS)"
    ),
)

TEST_DB_SUFFIX = "_anon012_clean_start"

if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

E2E_DSN: str | None = None


async def _make_e2e_database() -> str:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(REAL_DSN)
    e2e_dbname = f"{params.get('dbname', 'postgres')}{TEST_DB_SUFFIX}"
    params["dbname"] = "postgres"
    admin_dsn = make_conninfo(**params)
    conn = await psycopg.AsyncConnection.connect(admin_dsn, autocommit=True)
    try:
        await conn.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(e2e_dbname))
        )
    except psycopg.errors.DuplicateDatabase:
        pass
    finally:
        await conn.close()
    params["dbname"] = e2e_dbname
    return make_conninfo(**params)


@pytest.fixture(scope="module")
def e2e_dsn():
    global E2E_DSN
    if E2E_DSN is None:
        E2E_DSN = asyncio.run(_make_e2e_database())
    return E2E_DSN


@pytest.fixture
def app_env(e2e_dsn, monkeypatch):
    monkeypatch.setenv("GEMINI_GATEWAY_DATABASE_URL", e2e_dsn)
    return e2e_dsn


async def _drop_all_persistence_tables(dsn: str) -> None:
    """Guarantee a genuine clean start: NO persistence schema exists."""
    from core.resource_postgres import psycopg_async_connection_factory

    connection = await psycopg_async_connection_factory(dsn)()
    try:
        await connection.execute(
            "DROP TABLE IF EXISTS runtime_state;"
            "DROP TABLE IF EXISTS credentials;"
            "DROP TABLE IF EXISTS resource_definitions;"
        )
        await connection.commit()
    finally:
        await connection.close()


def _config() -> dict:
    return {
        "providers": {
            "antigravity": {
                "enabled": True,
                "resources": [{"id": "r1", "project_id": "p-clean"}],
            },
        },
        "resource_store": {"backend": "postgres"},
        "resource_bootstrap": {"enabled": True, "mode": "import"},
    }


def _columns(dsn: str, table: str) -> set:
    async def _run():
        from core.resource_postgres import psycopg_async_connection_factory

        connection = await psycopg_async_connection_factory(dsn)()
        try:
            cursor = await connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s",
                (table,),
            )
            rows = await cursor.fetchall()
            return {
                row[0] if isinstance(row, tuple) else row["column_name"]
                for row in rows
            }
        finally:
            await connection.close()

    return asyncio.run(_run())


def _table_exists(dsn: str, table: str) -> bool:
    async def _run():
        from core.resource_postgres import psycopg_async_connection_factory

        connection = await psycopg_async_connection_factory(dsn)()
        try:
            cursor = await connection.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
                (table,),
            )
            return bool(await cursor.fetchone())
        finally:
            await connection.close()

    return asyncio.run(_run())


def test_clean_start_completes_r6_migration_and_serves_runtime(app_env):
    from core.repositories.postgres import (
        PostgresResourceDefinitionRepository,
    )

    asyncio.run(_drop_all_persistence_tables(app_env))
    assert not _table_exists(app_env, "resource_definitions")

    # FULL production startup on a pristine database — no fixture
    # pre-initializes any schema.
    app = create_app(config=_config())

    try:
        # R6 columns present on the upgraded table
        columns = _columns(app_env, "resource_definitions")
        assert {"id", "created_at", "updated_at", "provider",
                "resource_id"} <= columns

        # credentials + runtime_state tables created
        assert _table_exists(app_env, "credentials")
        assert _table_exists(app_env, "runtime_state")

        # R7 source of record is the new-protocol repository
        assert isinstance(
            app.state.resource_definition_repository,
            PostgresResourceDefinitionRepository,
        )

        # the seed resource reached the runtime through the repository
        pool = app.state.scheduler.pools["antigravity"]
        assert [r.id for r in pool.resources] == ["r1"]

        # and it is queryable through the new-protocol repository
        async def _get():
            return await app.state.resource_definition_repository.get(
                "antigravity", "r1"
            )

        definition = asyncio.run(_get())
        assert definition is not None
        assert definition.id == "r1"
    finally:
        # drop the app's engine state before the next test reuses the DB
        asyncio.run(_drop_all_persistence_tables(app_env))


def test_postgres_start_without_bootstrap_still_migrates(app_env):
    """Migration is a startup concern, not a bootstrap concern."""
    asyncio.run(_drop_all_persistence_tables(app_env))

    config = _config()
    config["resource_bootstrap"] = {"enabled": False, "mode": "check"}
    app = create_app(config=config)

    try:
        columns = _columns(app_env, "resource_definitions")
        assert {"id", "created_at", "updated_at"} <= columns
        assert _table_exists(app_env, "credentials")
        assert _table_exists(app_env, "runtime_state")
        # bootstrap disabled: the runtime is built from the YAML config
        # (pre-006 legacy path) — the migration completed regardless
        assert app.state.scheduler.pools["antigravity"].resources
    finally:
        asyncio.run(_drop_all_persistence_tables(app_env))
