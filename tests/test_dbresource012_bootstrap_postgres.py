"""DB-RESOURCE-012 — bootstrap import workflow E2E (opt-in, REAL PG).

Freezes the deployment import path (Part A) as executable behaviour:

    config.yaml definitions
        →  bootstrap apply (mode: import | check; overwrite rejected at
          startup per CONFIG-001-ADR)
        →  PostgreSQLResourceRepository        (resource_store.backend: postgres)
        →  runtime reconcile
        →  Resource pool / scheduler

Scenarios against a REAL PostgreSQL server (full ``create_app`` chain,
not repository-level units):

1. empty PG + YAML        + mode=import    → PG contains the definitions,
                                             runtime built from them;
2. existing PG + same YAML+ mode=import    → restart: unchanged, no
                                             duplicate writes;
3. existing PG + drifted YAML + import     → conflict reported, DB NOT
                                             overwritten, runtime keeps
                                             the DB value (DB primary);
4. drifted YAML + overwrite                → startup rejected, DB
                                             untouched (CONFIG-003).

Part C (startup order) is exercised implicitly by every scenario:
``create_app`` runs  create sink → bootstrap apply → runtime reconcile →
build runtime  in one call, so a conflict scenario where the runtime
keeps the DB value proves apply ran before the runtime was built.

Isolation: dedicated database ``<dbname>_dbresource012_e2e`` (created
when absent); the operator DSN's database is never written.  Opt-in via
``GEMINI_GATEWAY_TEST_DATABASE_URL``; skipped cleanly without it.
"""

from __future__ import annotations

import asyncio
import logging
import os

import pytest

from app.main import create_app
from core.resource_bootstrap import ResourceBootstrapError
from core.resource_definition import (
    AntigravityResourceDefinition,
    GeminiCliResourceDefinition,
)
from core.resource_postgres import (
    PostgreSQLResourceRepository,
    psycopg_async_connection_factory,
)

REAL_DSN = os.environ.get("GEMINI_GATEWAY_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not REAL_DSN,
    reason=(
        "no real PostgreSQL server available; set GEMINI_GATEWAY_TEST_DATABASE_URL "
        "to opt in (this run is reported as an environment limitation, not a PASS)"
    ),
)

TEST_DB_SUFFIX = "_dbresource012_e2e"

# The resource store is ASYNC psycopg — on Windows the default
# ProactorEventLoop cannot drive it; use the Selector loop (see 010).
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


def _fresh_repo(dsn: str) -> PostgreSQLResourceRepository:
    return PostgreSQLResourceRepository(psycopg_async_connection_factory(dsn))


def _clear_table(dsn: str) -> None:
    async def _run() -> None:
        repo = _fresh_repo(dsn)
        await repo.initialize()  # table may not exist yet on first use
        connection = await repo._connection_factory()
        try:
            await connection.execute("DELETE FROM resource_definitions")
            await connection.commit()
        finally:
            await connection.close()

    asyncio.run(_run())


def _pg_definitions(dsn: str):
    async def _run():
        repo = _fresh_repo(dsn)
        return await repo.list()

    return asyncio.run(_run())


def _pg_definition(dsn: str, provider: str, rid: str):
    async def _run():
        repo = _fresh_repo(dsn)
        return await repo.get(provider, rid)

    return asyncio.run(_run())


def yaml_config(mode: str, project_id: str = "p-yaml") -> dict:
    """The deployment's config.yaml: two provider sections, one resource
    each (the second exists to prove multi-provider import)."""
    return {
        "providers": {
            "antigravity": {
                "enabled": True,
                "resources": [
                    {"id": "r1", "project_id": project_id},
                ],
            },
            "gemini_cli": {
                "enabled": True,
                "resources": [
                    {"id": "g1", "tier": "paid"},
                ],
            },
        },
        "resource_store": {"backend": "postgres"},
        "resource_bootstrap": {"enabled": True, "mode": mode},
    }


@pytest.fixture
def app_env(e2e_dsn, monkeypatch):
    """Point the shared DSN env var (credential-store precedent) at the
    dedicated E2E database for the duration of a test."""
    monkeypatch.setenv("GEMINI_GATEWAY_DATABASE_URL", e2e_dsn)
    return e2e_dsn


# -- 1. empty PG + YAML → import --------------------------------------------------


def test_empty_pg_import_populates_store_and_runtime(app_env):
    _clear_table(app_env)
    app = create_app(config=yaml_config("import"))

    result = app.state.resource_bootstrap_result
    assert result.mode.value == "import"
    assert [r.key for r in result.added] == [
        ("antigravity", "r1"),
        ("gemini_cli", "g1"),
    ]

    # The PG store contains exactly the imported definitions.
    stored = {(d.provider, d.id): d for d in _pg_definitions(app_env)}
    assert set(stored) == {("antigravity", "r1"), ("gemini_cli", "g1")}
    assert stored[("antigravity", "r1")].project_id == "p-yaml"

    # Runtime was built FROM the store (reconcile → build order).
    pool = app.state.scheduler.pools["antigravity"]
    assert [r.id for r in pool.resources] == ["r1"]
    assert pool.resources[0].project_id == "p-yaml"
    gpool = app.state.scheduler.pools["gemini_cli"]
    assert [r.id for r in gpool.resources] == ["g1"]
    assert gpool.resources[0].tier == "paid"


# -- 2. existing PG + same YAML → unchanged (restart) --------------------------------


def test_restart_with_same_yaml_is_unchanged(app_env):
    _clear_table(app_env)
    first = create_app(config=yaml_config("import"))
    assert len(first.state.resource_bootstrap_result.added) == 2

    # Restart: brand-new app instance over the same database.
    second = create_app(config=yaml_config("import"))
    result = second.state.resource_bootstrap_result
    assert [r.key for r in result.added] == []
    assert [r.key for r in result.unchanged] == [
        ("antigravity", "r1"),
        ("gemini_cli", "g1"),
    ]
    # Runtime is rebuilt from the PG store, identical content.
    pool = second.state.scheduler.pools["antigravity"]
    assert [r.id for r in pool.resources] == ["r1"]
    assert pool.resources[0].project_id == "p-yaml"


# -- 3. conflict + import → conflict, DB primary --------------------------------------


def test_conflict_import_never_overwrites_database(app_env):
    _clear_table(app_env)
    # The durable store holds a previous deployment's version...
    async def _seed():
        repo = _fresh_repo(app_env)
        await repo.add(
            AntigravityResourceDefinition(
                id="r1", enabled=False, credential_id="cred-db",
                project_id="p-db",
            )
        )

    asyncio.run(_seed())

    # ...and the new config.yaml drifted away from it.
    app = create_app(config=yaml_config("import", project_id="p-yaml"))
    result = app.state.resource_bootstrap_result
    assert [c.key for c in result.conflicts] == [("antigravity", "r1")]

    # DB primary: the stored definition is untouched...
    stored = _pg_definition(app_env, "antigravity", "r1")
    assert stored.project_id == "p-db"
    assert stored.enabled is False
    # ...and the runtime reflects the DB, not YAML.
    pool = app.state.scheduler.pools["antigravity"]
    assert pool.resources[0].project_id == "p-db"
    assert pool.resources[0].enabled is False

    # The second (gemini_cli) definition was still imported: conflicts
    # never block the rest of the plan.
    assert ("gemini_cli", "g1") in {
        (d.provider, d.id) for d in _pg_definitions(app_env)
    }


# -- 4. conflict + overwrite → rejected as a startup mode -------------------------------


def test_overwrite_startup_mode_rejected_database_untouched(app_env):
    """CONFIG-003 / CONFIG-001-ADR §2: `mode: overwrite` is no longer a
    legal persistent startup policy — startup fails closed and the
    repository is NOT touched.  (The OVERWRITE service capability itself
    remains, reserved for the explicit one-shot import command; its
    semantics are covered by tests/core/test_resource_bootstrap.py.)"""
    _clear_table(app_env)

    async def _seed():
        repo = _fresh_repo(app_env)
        await repo.add(
            AntigravityResourceDefinition(
                id="r1", enabled=False, credential_id="cred-db",
                project_id="p-db",
            )
        )

    asyncio.run(_seed())

    with pytest.raises(
        ResourceBootstrapError,
        match="no longer supported as a startup policy",
    ):
        create_app(config=yaml_config("overwrite", project_id="p-yaml"))

    # Fail-closed: the DB row survived untouched.
    stored = _pg_definition(app_env, "antigravity", "r1")
    assert stored.project_id == "p-db"
    assert stored.enabled is False


# -- check mode: plan only, no writes ---------------------------------------------------


def test_check_mode_writes_nothing(app_env, caplog):
    _clear_table(app_env)
    with caplog.at_level(logging.WARNING):
        app = create_app(config=yaml_config("check"))
    result = app.state.resource_bootstrap_result
    assert result.mode.value == "check"
    assert [r.key for r in result.added] == [
        ("antigravity", "r1"),
        ("gemini_cli", "g1"),
    ]
    assert _pg_definitions(app_env) == []
    # Runtime is still built (empty snapshot → empty pools).
    assert list(app.state.scheduler.pools["antigravity"].resources) == []
    # TASK-CONFIG-002 Part D: check + empty repository + non-empty seed
    # is a deployment footgun — the guidance warning must fire.
    assert any(
        "check mode did not import resources" in record.getMessage()
        for record in caplog.records
    )
