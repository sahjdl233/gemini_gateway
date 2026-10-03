"""CONTROL-001 / DB-RESOURCE-014 — multi-provider Admin API on REAL PG.

The control plane addresses resources by the full repository identity
``(provider, resource_id)``; PostgreSQL stores both rows; the runtime
snapshot keeps the pools independent:

1. POST r1 (antigravity) + POST r1 (gemini_cli) → TWO rows in PG;
2. PATCH gemini_cli/r1 → only that identity changes;
3. DELETE antigravity/r1 → gemini_cli/r1 survives;
4. runtime snapshot: resources_by_provider == {antigravity: [r1],
   gemini_cli: [r1]}.

Isolation: dedicated database ``<dbname>_control001_e2e``.  Opt-in via
``GEMINI_GATEWAY_TEST_DATABASE_URL``; skipped cleanly without it.
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

TEST_DB_SUFFIX = "_control001_e2e"

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


def _pg_keys(dsn: str) -> set:
    async def _run():
        repo = _fresh_repo(dsn)
        return {(d.provider, d.id) for d in await repo.list()}

    return asyncio.run(_run())


@pytest.fixture
def client(e2e_dsn, monkeypatch):
    monkeypatch.setenv("GEMINI_GATEWAY_DATABASE_URL", e2e_dsn)
    monkeypatch.setenv("ADMIN_TOKEN", "e2e-admin-token")
    _clear_table(e2e_dsn)
    app = create_app(
        config={
            "providers": {
                "antigravity": {"enabled": True, "resources": []},
                "gemini_cli": {"enabled": True, "resources": []},
            },
            "resource_store": {"backend": "postgres"},
            "resource_bootstrap": {"enabled": True, "mode": "import"},
        }
    )
    return TestClient(app)


def _create(client: TestClient, provider: str, **fields):
    return client.post(
        "/admin/resources",
        json={"provider": provider, "id": "r1", **fields},
        headers=ADMIN,
    )


def test_same_id_two_providers_two_rows(client, e2e_dsn):
    first = _create(client, "antigravity", project_id="p-ant")
    second = _create(client, "gemini_cli", tier="paid")
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    # PG holds TWO rows — the composite identity allows the same id.
    assert _pg_keys(e2e_dsn) == {
        ("antigravity", "r1"),
        ("gemini_cli", "r1"),
    }

    # Runtime snapshot (the manager keeps the latest reconcile result):
    # both pools have their own r1.
    snapshot = client.app.state.resource_manager.last_snapshot
    assert {p: [r.id for r in rs]
            for p, rs in snapshot.resources_by_provider.items()} == {
        "antigravity": ["r1"],
        "gemini_cli": ["r1"],
    }
    ant = client.app.state.scheduler.pools["antigravity"].resources[0]
    gem = client.app.state.scheduler.pools["gemini_cli"].resources[0]
    assert ant.project_id == "p-ant"
    assert gem.tier == "paid"


def test_scoped_patch_hits_only_one_identity(client, e2e_dsn):
    _create(client, "antigravity", project_id="p-ant")
    _create(client, "gemini_cli", tier="paid")

    response = client.patch(
        "/admin/resources/gemini_cli/r1", json={"tier": "free"}, headers=ADMIN
    )
    assert response.status_code == 200, response.text

    async def _read():
        repo = _fresh_repo(e2e_dsn)
        return await repo.list()

    rows = {(d.provider, d.id): d for d in asyncio.run(_read())}
    # Only gemini_cli/r1 changed...
    assert rows[("gemini_cli", "r1")].tier == "free"
    # ...antigravity/r1 is untouched.
    assert rows[("antigravity", "r1")].project_id == "p-ant"
    # Runtime reflects exactly the addressed change.
    gem = client.app.state.scheduler.pools["gemini_cli"].resources[0]
    ant = client.app.state.scheduler.pools["antigravity"].resources[0]
    assert gem.tier == "free"
    assert ant.project_id == "p-ant"


def test_scoped_delete_spares_the_other_provider(client, e2e_dsn):
    _create(client, "antigravity", project_id="p-ant")
    _create(client, "gemini_cli", tier="paid")

    response = client.delete(
        "/admin/resources/antigravity/r1", headers=ADMIN
    )
    assert response.status_code == 204

    # gemini_cli/r1 survives in PG and in the runtime.
    assert _pg_keys(e2e_dsn) == {("gemini_cli", "r1")}
    pool = client.app.state.scheduler.pools["gemini_cli"].resources
    assert [r.id for r in pool] == ["r1"]
    assert client.app.state.scheduler.pools["antigravity"].resources == []


def test_runtime_snapshot_shape(client, e2e_dsn):
    _create(client, "antigravity", project_id="p-ant")
    _create(client, "gemini_cli", tier="paid")

    snapshot = client.app.state.resource_manager.last_snapshot
    assert snapshot.source_count == 2
    assert {(d.provider, d.id) for d in snapshot.resources} == {
        ("antigravity", "r1"),
        ("gemini_cli", "r1"),
    }
