"""CONFIG/R-6 — PostgreSQL persistence adapters E2E (opt-in, REAL PG).

Runs the SAME :class:`RepositoryContractSuite` as the memory tier against
the PostgreSQL adapters:

    ResourceDefinitionRepository ← resource_definitions (upgraded table)
    CredentialRepository         ← credentials (AUTH-009, same encryptor)
    RuntimeStateStore            ← runtime_state (new table)

via a dedicated database ``<dbname>_r6_e2e`` (created when absent; the
operator DSN's database is never written).  Skips cleanly without
GEMINI_GATEWAY_TEST_DATABASE_URL.

Extra coverage beyond the shared suite:

* the idempotent schema migration is compatible with the EXISTING
  ``PostgreSQLResourceRepository`` startup path (the upgraded table must
  keep serving it unchanged);
* RuntimeReconciliationService reconciles straight from the PostgreSQL
  definition repository — the domain layer never sees the database.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from core.credential_encryption import CredentialEncryptor
from core.credential_postgres import (
    PostgreSQLCredentialRepository,
    psycopg_connection_factory,
)
from core.repositories.memory import (  # noqa: F401  (suite lives here)
    MemoryCredentialRepository,
)
from core.repositories.postgres import (
    PostgresCredentialRepository,
    PostgresResourceDefinitionRepository,
    PostgresRuntimeStateStore,
    initialize_persistence,
)
from core.resource_postgres import (
    PostgreSQLResourceRepository,
    psycopg_async_connection_factory,
)
from core.runtime_reconciliation import RuntimeReconciliationService
from tests.core.test_repository_contract import RepositoryContractSuite

REAL_DSN = os.environ.get("GEMINI_GATEWAY_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not REAL_DSN,
    reason=(
        "no real PostgreSQL server available; set GEMINI_GATEWAY_TEST_DATABASE_URL "
        "to opt in (skipped as an environment limitation, not a failure)"
    ),
)

TEST_DB_SUFFIX = "_r6_e2e"

# Async psycopg on Windows needs the Selector loop (see dbresource010).
if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

E2E_DSN: str | None = None


async def _make_e2e_database() -> str:
    """Create the dedicated E2E database when absent; return its DSN."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(REAL_DSN)
    e2e_dbname = f"{params.get('dbname', 'postgres')}{TEST_DB_SUFFIX}"
    params["dbname"] = "postgres"  # maintenance DB only
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


def _async_factory(dsn: str):
    return psycopg_async_connection_factory(dsn)


def _clear_tables(dsn: str) -> None:
    """Run the idempotent migration and clear all three tables."""
    async def _run():
        await initialize_persistence(_async_factory(dsn))
        connection = await _async_factory(dsn)()
        try:
            for table in (
                "resource_definitions", "credentials", "runtime_state"
            ):
                await connection.execute(f"DELETE FROM {table}")
            await connection.commit()
        finally:
            await connection.close()

    asyncio.run(_run())


@pytest.fixture
def clean_database(e2e_dsn):
    _clear_tables(e2e_dsn)
    return e2e_dsn


def _postgres_suite(dsn: str) -> RepositoryContractSuite:
    encryptor = CredentialEncryptor(os.urandom(32))
    credential_inner = PostgreSQLCredentialRepository(
        psycopg_connection_factory(dsn), encryptor=encryptor
    )
    return RepositoryContractSuite(
        definitions=PostgresResourceDefinitionRepository(_async_factory(dsn)),
        credentials=PostgresCredentialRepository(credential_inner),
        states=PostgresRuntimeStateStore(_async_factory(dsn)),
        label="postgres",
    )


def test_postgres_persistence_contract(clean_database):
    suite = _postgres_suite(clean_database)
    asyncio.run(suite.run_all())
    # Run it twice — each on a CLEAN store — so the contract holds both
    # on first insert and on a database that already went through a run
    # (replace/upsert semantics against leftover state are covered by
    # the replace checks inside the suite itself).
    _clear_tables(clean_database)
    asyncio.run(suite.run_all())


def test_migration_is_idempotent_and_keeps_legacy_repo_compatible(
    clean_database,
):
    """Re-running the migration is a no-op, and the upgraded
    resource_definitions table still serves the pre-R-6 startup
    repository unchanged (structural compatibility guarantee)."""
    async def _run():
        await initialize_persistence(_async_factory(clean_database))
        await initialize_persistence(_async_factory(clean_database))

        legacy = PostgreSQLResourceRepository(_async_factory(clean_database))
        await legacy.initialize()

        from core.resource_definition import AntigravityResourceDefinition

        await legacy.add(
            AntigravityResourceDefinition(
                provider="antigravity", id="legacy-1", project_id="p-legacy"
            )
        )
        stored = await legacy.get("antigravity", "legacy-1")
        assert stored is not None
        assert stored.project_id == "p-legacy"
        return await legacy.list()

    listing = asyncio.run(_run())
    assert [d.id for d in listing] == ["legacy-1"]


def test_reconciliation_reads_postgres_repository(clean_database):
    """RuntimeReconciliationService reconciles straight from the
    PostgreSQL definition repository — the domain layer never sees the
    database (no psycopg import, no table knowledge)."""
    async def _run():
        repo = PostgresResourceDefinitionRepository(_async_factory(clean_database))
        from core.resource_definition import AntigravityResourceDefinition

        await repo.save(
            AntigravityResourceDefinition(
                provider="antigravity", id="r1", project_id="p-live"
            )
        )

        def builder(definition_list):
            from core.resource import Resource

            return {
                "antigravity": [
                    Resource(id=d.id, provider=d.provider)
                    for d in definition_list
                ]
            }

        service = RuntimeReconciliationService(repo, builder)
        snapshot = await service.reconcile()
        resources = snapshot.resources_by_provider["antigravity"]
        assert [r.id for r in resources] == ["r1"]
        # And the state store is empty but harmless — optional by contract.
        states = PostgresRuntimeStateStore(_async_factory(clean_database))
        from core.resource import ResourceKey

        assert await states.get(ResourceKey("antigravity", "r1")) is None

    asyncio.run(_run())
