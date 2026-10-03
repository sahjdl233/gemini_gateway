"""CONTROL-002 — provider-aware resource READ API on REAL PG (opt-in).

Freezes the read side of the control plane:

    Admin API → ResourceRepository → ResourceDefinition → serialization

with runtime observability merged best-effort and fully independent of
the definition state.

Scenarios (postgres backend, bootstrap import, two providers sharing
the same resource_id):

1. GET /resources                 → both providers, definition state;
2. GET /resources?provider=gemini_cli → scoped listing;
3. GET /resources/{provider}/{id} → 200 / 404 / unknown-provider 400;
4. secrets never appear in responses;
5. runtime counters (total_requests/health) and definition state are
   independent: changing pool counters does not change definition
   fields, and a DIRECT database mutation (bypassing the app) shows up
   in the read API even though the runtime pool still holds the old
   configuration — the read path is the repository, not the pool;
6. a freshly constructed repository reads exactly what the API returns.

Isolation: dedicated database ``<dbname>_control002_e2e``.  Opt-in via
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

TEST_DB_SUFFIX = "_control002_e2e"

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


def _fresh_repo(dsn: str):
    from core.resource_postgres import (
        PostgreSQLResourceRepository,
        psycopg_async_connection_factory,
    )

    return PostgreSQLResourceRepository(psycopg_async_connection_factory(dsn))


@pytest.fixture
def client(e2e_dsn, monkeypatch):
    monkeypatch.setenv("GEMINI_GATEWAY_DATABASE_URL", e2e_dsn)
    monkeypatch.setenv("ADMIN_TOKEN", "e2e-admin-token")
    _clear_table(e2e_dsn)  # the dedicated DB persists across runs
    app = create_app(
        config={
            "providers": {
                "antigravity": {
                    "enabled": True,
                    "resources": [{"id": "r1", "project_id": "p-ant"}],
                },
                "gemini_cli": {
                    "enabled": True,
                    "resources": [{"id": "r1", "tier": "paid"}],
                },
            },
            "resource_store": {"backend": "postgres"},
            "resource_bootstrap": {"enabled": True, "mode": "import"},
        }
    )
    return TestClient(app)


def test_listing_returns_both_providers_with_same_resource_id(client):
    listing = client.get("/admin/resources", headers=ADMIN)
    assert listing.status_code == 200
    items = listing.json()
    assert {(i["provider"], i["id"]) for i in items} == {
        ("antigravity", "r1"),
        ("gemini_cli", "r1"),
    }
    by_provider = {i["provider"]: i for i in items}
    assert by_provider["antigravity"]["project_id"] == "p-ant"
    assert by_provider["gemini_cli"]["tier"] == "paid"
    assert by_provider["gemini_cli"]["enabled"] is True


def test_provider_scoped_listing_via_query_param(client):
    scoped = client.get(
        "/admin/resources", params={"provider": "gemini_cli"}, headers=ADMIN
    )
    assert scoped.status_code == 200
    items = scoped.json()
    assert [i["provider"] for i in items] == ["gemini_cli"]

    empty = client.get(
        "/admin/resources", params={"provider": "firebase"}, headers=ADMIN
    )
    assert empty.status_code == 200
    assert empty.json() == []


def test_scoped_get_hit_miss_and_unknown_provider(client):
    hit = client.get("/admin/resources/gemini_cli/r1", headers=ADMIN)
    assert hit.status_code == 200
    assert hit.json()["tier"] == "paid"

    miss = client.get("/admin/resources/gemini_cli/nope", headers=ADMIN)
    assert miss.status_code == 404
    assert miss.json()["detail"] == "resource not found"

    unknown = client.get(
        "/admin/resources/does_not_exist/r1", headers=ADMIN
    )
    assert unknown.status_code == 400
    assert "unknown or unmanaged provider" in unknown.json()["detail"]


def test_secrets_never_enter_the_response(client):
    listing = client.get("/admin/resources", headers=ADMIN)
    body = listing.text
    for forbidden in ("access_token", "refresh_token", "client_secret",
                      "api_key", "token_expiry"):
        assert f'"{forbidden}"' not in body, forbidden
    # And the antigravity item still carries its definition fields.
    items = listing.json()
    ant = next(i for i in items if i["provider"] == "antigravity")
    assert ant["project_id"] == "p-ant"
    assert ant["ide_type"] == "ANTIGRAVITY"


def test_runtime_counters_and_definition_state_are_independent(
    client, e2e_dsn
):
    """Part D — the read path is the repository, not the pool.

    1. Mutate ONLY the live pool's runtime counters: definition fields
       in the response are unchanged (they come from the repository).
    2. Mutate the DATABASE directly (bypassing the app): the response
       reflects the new definition state even though the runtime pool
       still holds the old configuration.
    """
    pool = client.app.state.scheduler.pools["gemini_cli"].resources
    pool[0].total_requests = 42
    pool[0].total_failures = 3

    before = client.get("/admin/resources/gemini_cli/r1", headers=ADMIN).json()
    assert before["total_requests"] == 42  # runtime view
    assert before["tier"] == "paid"  # definition view, unchanged

    # Direct DB mutation, invisible to the app's runtime.
    async def _bypass_update():
        from core.resource_definition import GeminiCliResourceDefinition

        repo = _fresh_repo(e2e_dsn)
        await repo.update(
            GeminiCliResourceDefinition(
                id="r1",
                enabled=False,
                credential_id=None,
                tier="free",
            )
        )

    asyncio.run(_bypass_update())

    after = client.get("/admin/resources/gemini_cli/r1", headers=ADMIN).json()
    # Definition state tracks the REPOSITORY, not the pool...
    assert after["tier"] == "free"
    assert after["enabled"] is False
    # ...while the runtime counters remain the pool's own values.
    assert after["total_requests"] == 42
    assert after["total_failures"] == 3

    # The antigravity identity is untouched by all of this.
    ant = client.get("/admin/resources/antigravity/r1", headers=ADMIN).json()
    assert ant["project_id"] == "p-ant"
    assert ant["enabled"] is True


def test_fresh_repository_reads_match_the_api(client, e2e_dsn):
    """A newly constructed repository sees exactly what the read API
    returns (definition fields)."""
    api_items = client.get("/admin/resources", headers=ADMIN).json()
    api_view = {(i["provider"], i["id"]): i for i in api_items}

    async def _read():
        repo = _fresh_repo(e2e_dsn)
        return await repo.list()

    rows = {(d.provider, d.id): d for d in asyncio.run(_read())}
    assert set(rows) == set(api_view)
    for key, definition in rows.items():
        item = api_view[key]
        assert item["enabled"] == definition.enabled
        assert item["credential_id"] == definition.credential_id
        # Provider-specific body round-trips identically.
        body = definition.to_definition_json()
        for field, value in body.items():
            assert item[field] == value, (key, field)
