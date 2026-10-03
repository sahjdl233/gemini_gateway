"""DB-RESOURCE-013 — Admin write path over REAL PostgreSQL (opt-in).

Proves the control-plane write path end to end on a real server:

    Admin API (/admin/resources)
        → PostgreSQLResourceRepository       (resource_store.backend: postgres)
        → RuntimeReconciliationService
        → runtime snapshot / pools

Flow per test: the fixture clears the dedicated database, then
``create_app`` bootstraps the YAML seed (r1) into PostgreSQL and builds
the runtime from it; the Admin API then mutates the store and every
assertion cross-checks BOTH PostgreSQL (fresh repository, bypassing the
app) and the live runtime pool.

Scenarios:

1. POST  → row in PG + resource in the pool (runtime changed by API);
2. PATCH → row updated in PG + runtime updated, runtime state preserved
   across the reconciliation (per ResourceKey);
3. DELETE → row gone from PG + resource gone from the pool;
4. duplicate create → 400; unknown update/delete → 404; secret fields → 400.

Isolation: dedicated database ``<dbname>_dbresource013_e2e``.  Opt-in
via ``GEMINI_GATEWAY_TEST_DATABASE_URL``; skipped cleanly without it.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

REAL_DSN = os.environ.get("GEMINI_GATEWAY_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not REAL_DSN,
    reason=(
        "no real PostgreSQL server available; set GEMINI_GATEWAY_TEST_DATABASE_URL "
        "to opt in (this run is reported as an environment limitation, not a PASS)"
    ),
)

TEST_DB_SUFFIX = "_dbresource013_e2e"

if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

E2E_DSN: str | None = None
ADMIN = {"Authorization": "Bearer e2e-admin-token"}


async def _make_e2e_database() -> str:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(REAL_DSN)
    e2e_dbname = f"{params.get('dbname', 'postgres')}{TEST_DB_SUFFIX}"
    params["dbname"] = "postgres"
    conn = await psycopg.AsyncConnection.connect(
        make_conninfo(**params), autocommit=True
    )
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


def _fresh_repo(dsn: str):
    from core.resource_postgres import (
        PostgreSQLResourceRepository,
        psycopg_async_connection_factory,
    )

    return PostgreSQLResourceRepository(psycopg_async_connection_factory(dsn))


def _clear_table(dsn: str) -> None:
    async def _run() -> None:
        repo = _fresh_repo(dsn)
        await repo.initialize()
        connection = await repo._connection_factory()
        try:
            await connection.execute("DELETE FROM resource_definitions")
            await connection.commit()
        finally:
            await connection.close()

    asyncio.run(_run())


def _pg_definition(dsn: str, rid: str):
    """Read the stored definition through a FRESH repository — proof the
    row is really in PostgreSQL, not just in the app's state."""

    async def _run():
        repo = _fresh_repo(dsn)
        return await repo.get("antigravity", rid)

    return asyncio.run(_run())


@pytest.fixture
def client(e2e_dsn, monkeypatch):
    monkeypatch.setenv("GEMINI_GATEWAY_DATABASE_URL", e2e_dsn)
    monkeypatch.setenv("ADMIN_TOKEN", "e2e-admin-token")
    _clear_table(e2e_dsn)
    app = create_app(
        config={
            "providers": {
                "antigravity": {
                    "enabled": True,
                    "resources": [{"id": "r1", "project_id": "p-seed"}],
                }
            },
            "resource_store": {"backend": "postgres"},
            "resource_bootstrap": {"enabled": True, "mode": "import"},
        }
    )
    return TestClient(app)


def _dsn() -> str:
    assert E2E_DSN is not None
    return E2E_DSN


def test_bootstrap_imported_seed_into_postgres(client):
    """Baseline for the write-path scenarios: the YAML seed landed in PG
    and the runtime was built from it."""
    stored = _pg_definition(_dsn(), "r1")
    assert stored is not None and stored.project_id == "p-seed"
    pool = client.app.state.scheduler.pools["antigravity"].resources
    assert [r.id for r in pool] == ["r1"]
    assert pool[0].project_id == "p-seed"


def test_admin_create_lands_in_postgres_and_runtime(client):
    response = client.post(
        "/admin/resources",
        json={"id": "r2", "project_id": "p2"},
        headers=ADMIN,
    )
    assert response.status_code == 201, response.text

    # The row is in PostgreSQL...
    stored = _pg_definition(_dsn(), "r2")
    assert stored is not None
    assert stored.project_id == "p2"
    # ...and the runtime changed because of the API call.
    pool = client.app.state.scheduler.pools["antigravity"].resources
    assert [r.id for r in pool] == ["r1", "r2"]
    assert pool[1].project_id == "p2"


def test_admin_patch_updates_postgres_and_runtime_state_preserved(client):
    pool_resources = client.app.state.scheduler.pools["antigravity"].resources
    pool_resources[0].total_requests = 9
    pool_resources[0].total_failures = 2

    response = client.patch(
        "/admin/resources/r1",
        json={"project_id": "p-edited"},
        headers=ADMIN,
    )
    assert response.status_code == 200, response.text

    stored = _pg_definition(_dsn(), "r1")
    assert stored.project_id == "p-edited"
    resource = client.app.state.scheduler.pools["antigravity"].resources[0]
    assert resource.project_id == "p-edited"
    # Runtime state preserved across the reconciliation (per ResourceKey).
    assert resource.total_requests == 9
    assert resource.total_failures == 2


def test_admin_delete_removes_from_postgres_and_runtime(client):
    response = client.post(
        "/admin/resources",
        json={"id": "r2", "project_id": "p2"},
        headers=ADMIN,
    )
    assert response.status_code == 201

    response = client.delete("/admin/resources/r2", headers=ADMIN)
    assert response.status_code == 204
    assert _pg_definition(_dsn(), "r2") is None
    pool = client.app.state.scheduler.pools["antigravity"].resources
    assert [r.id for r in pool] == ["r1"]


def test_admin_error_semantics(client):
    dup = client.post(
        "/admin/resources",
        json={"id": "r1", "project_id": "other"},
        headers=ADMIN,
    )
    assert dup.status_code == 400
    assert "already exists" in dup.json()["detail"]

    missing_patch = client.patch(
        "/admin/resources/nope", json={"project_id": "x"}, headers=ADMIN
    )
    assert missing_patch.status_code == 404

    missing_delete = client.delete(
        "/admin/resources/nope", headers=ADMIN
    )
    assert missing_delete.status_code == 404

    secret = client.post(
        "/admin/resources",
        json={"id": "r9", "access_token": "tok"},
        headers=ADMIN,
    )
    assert secret.status_code == 400
