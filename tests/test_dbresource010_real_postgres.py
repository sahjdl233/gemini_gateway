"""DB-RESOURCE-010 — REAL PostgreSQL definition store E2E (opt-in).

Proves the existing ``PostgreSQLResourceRepository`` (001-4; the ADR-003
designated durable definition store) against a REAL PostgreSQL server:

    schema initialize (repeatable)
      → add / get round trip (DTO equality, JSONB body)
      → duplicate composite key → DuplicateResourceDefinitionError
      → update unknown → UnknownResourceDefinitionError (no upsert)
      → delete existing / idempotent
      → read-side adapter path (ResourceRepositoryDefinitionSource)
      → full restart recovery (fresh repository, schema repeatable)

Unit-level semantics are already frozen by
``tests/core/test_resource_repository_contract.py`` (fake driver); this
suite verifies the same semantics against real SQL.

Isolation (AUTH-015-FIX-01 pattern): the suite NEVER touches the
database named in ``GEMINI_GATEWAY_TEST_DATABASE_URL``.  It derives a
DEDICATED test database (``<dbname>_dbresource010_e2e``, created
automatically when absent) and every statement executes only there.

Opt-in: set ``GEMINI_GATEWAY_TEST_DATABASE_URL``.  Skipped — and
reported as such — when no server is available.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from core.resource_definition import (
    AntigravityResourceDefinition,
    GeminiCliResourceDefinition,
)
from core.resource_definition_repository import (
    ResourceRepositoryDefinitionSource,
)
from core.resource_postgres import (
    PostgreSQLResourceRepository,
    psycopg_async_connection_factory,
)
from core.resource_repository import (
    DuplicateResourceDefinitionError,
    UnknownResourceDefinitionError,
)

REAL_DSN = os.environ.get("GEMINI_GATEWAY_TEST_DATABASE_URL")

# The resource store is ASYNC psycopg (unlike the sync credential
# repository AUTH-015 exercised).  On Windows the default ProactorEventLoop
# cannot drive async psycopg — switch this process to the Selector loop.
if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytestmark = pytest.mark.skipif(
    not REAL_DSN,
    reason=(
        "no real PostgreSQL server available; set GEMINI_GATEWAY_TEST_DATABASE_URL "
        "to opt in (this run is reported as an environment limitation, not a PASS)"
    ),
)

TEST_DB_SUFFIX = "_dbresource010_e2e"

E2E_DSN: str | None = None


async def _make_e2e_database() -> str:
    """Create the dedicated E2E database when absent; return its DSN."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(REAL_DSN)
    e2e_dbname = f"{params.get('dbname', 'postgres')}{TEST_DB_SUFFIX}"
    params["dbname"] = "postgres"  # connect to the maintenance DB
    admin_dsn = make_conninfo(**params)
    conn = await psycopg.AsyncConnection.connect(admin_dsn, autocommit=True)
    try:
        await conn.execute(
            sql.SQL("CREATE DATABASE {}").format(
                sql.Identifier(e2e_dbname)
            )
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
async def repo(e2e_dsn):
    repository = PostgreSQLResourceRepository(
        psycopg_async_connection_factory(e2e_dsn)
    )
    await repository.initialize()
    yield repository


def antigravity_def(rid: str = "r1", project_id: str = "p1"):
    return AntigravityResourceDefinition(
        id=rid, enabled=True, credential_id="cred-1", project_id=project_id
    )


def gemini_def(rid: str = "g1", tier: str = "paid"):
    return GeminiCliResourceDefinition(
        id=rid, enabled=False, credential_id=None, project_id="p",
        tier=tier,
    )


async def _clear_table(repository: PostgreSQLResourceRepository) -> None:
    async def _clear() -> None:
        connection = await repository._connection_factory()
        try:
            await connection.execute("DELETE FROM resource_definitions")
            await connection.commit()
        finally:
            await connection.close()

    await _clear()


# -- schema / initialize --------------------------------------------------------


async def test_initialize_is_repeatable_on_real_server(repo):
    await repo.initialize()
    await repo.initialize()


# -- CRUD round trip ---------------------------------------------------------------


async def test_add_get_round_trip_preserves_dto(repo):
    await _clear_table(repo)
    defn = gemini_def(rid="g1", tier="paid")
    await repo.add(defn)
    stored = await repo.get("gemini_cli", "g1")
    assert stored == defn
    assert stored.tier == "paid"
    assert stored.enabled is False


async def test_duplicate_composite_key_raises(repo):
    await _clear_table(repo)
    await repo.add(antigravity_def(rid="r1"))
    with pytest.raises(DuplicateResourceDefinitionError) as exc:
        await repo.add(antigravity_def(rid="r1", project_id="p-other"))
    assert exc.value.provider == "antigravity"
    assert exc.value.resource_id == "r1"
    # Original untouched.
    stored = await repo.get("antigravity", "r1")
    assert stored.project_id == "p1"


async def test_update_unknown_raises_no_upsert(repo):
    await _clear_table(repo)
    with pytest.raises(UnknownResourceDefinitionError):
        await repo.update(antigravity_def(rid="ghost"))


async def test_delete_existing_and_idempotent(repo):
    await _clear_table(repo)
    await repo.add(antigravity_def(rid="r1"))
    await repo.delete("antigravity", "r1")
    assert await repo.get("antigravity", "r1") is None
    await repo.delete("antigravity", "r1")  # no raise


# -- read-side adapter path (Part C) -------------------------------------------------


async def test_definition_source_adapter_over_real_store(repo):
    await _clear_table(repo)
    await repo.add(antigravity_def(rid="r1"))
    await repo.add(gemini_def(rid="g1"))
    source = ResourceRepositoryDefinitionSource(repo)
    definitions = await source.list_definitions()
    assert [(d.provider, d.id) for d in definitions] == [
        ("antigravity", "r1"),
        ("gemini_cli", "g1"),
    ]
    found = await source.get_definition("gemini_cli", "g1")
    assert type(found) is GeminiCliResourceDefinition


# -- restart recovery -----------------------------------------------------------------


async def test_restart_recovery_with_fresh_repository(repo, e2e_dsn):
    await _clear_table(repo)
    await repo.add(antigravity_def(rid="keep"))
    # A "restart": brand-new repository instance over the same DSN.
    fresh = PostgreSQLResourceRepository(
        psycopg_async_connection_factory(e2e_dsn)
    )
    await fresh.initialize()
    stored = await fresh.get("antigravity", "keep")
    assert stored == antigravity_def(rid="keep")
