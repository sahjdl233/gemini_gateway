"""CONFIG/R-7: persistence wiring & repository cutover.

Freezes the production wiring after the cutover:

* the postgres definition source of record IS the new persistence-layer
  adapter (``ResourceDefinitionRepository`` protocol) — the deprecated
  ``PostgreSQLResourceRepository`` read adapter is no longer on that
  path;
* the old repository is retained (deprecated, warning-emitting) as the
  bootstrap/ResourceManager write sink — never deleted;
* the startup composition has exactly ONE reconciliation service, fed
  by the repository — never a definitions list, and never two sources.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from core.resource_definition_repository import (
    ResourceRepositoryDefinitionSource,
)
from core.resource_definition_repository_factory import (
    create_resource_definition_repository,
)
from core.resource_postgres import PostgreSQLResourceRepository

DSN = "postgresql://gw:gwpass@localhost:15432/gw_resources"


@pytest.fixture
def dsn_env(monkeypatch):
    monkeypatch.setenv("GEMINI_GATEWAY_DATABASE_URL", DSN)


# -- Test 1: startup uses the repository (factory cutover) --------------------------


def test_postgres_startup_source_is_the_protocol_repository(dsn_env):
    from core.repositories import ResourceDefinitionRepository
    from core.repositories.postgres import (
        PostgresResourceDefinitionRepository,
    )

    repo = create_resource_definition_repository(
        {"resource_store": {"backend": "postgres"}}
    )
    assert isinstance(repo, ResourceDefinitionRepository)
    assert isinstance(repo, PostgresResourceDefinitionRepository)
    assert not isinstance(repo, ResourceRepositoryDefinitionSource)


def test_memory_startup_source_is_unchanged():
    """The memory backend keeps its config-seed source (legacy read
    protocol) — the cutover targets postgres production wiring only."""
    repo = create_resource_definition_repository(
        {"providers": {"antigravity": {"resources": [{"id": "r1"}]}}}
    )
    assert isinstance(repo, ResourceRepositoryDefinitionSource) or type(
        repo
    ).__name__ == "ConfigResourceDefinitionRepository"


def test_postgres_without_dsn_still_fails_closed(monkeypatch):
    monkeypatch.delenv("GEMINI_GATEWAY_DATABASE_URL", raising=False)
    with pytest.raises(Exception, match="GEMINI_GATEWAY_DATABASE_URL"):
        create_resource_definition_repository(
            {"resource_store": {"backend": "postgres"}}
        )


# -- Test 2: the old repository is retained, deprecated — not deleted ----------------


def test_old_repository_is_deprecated_but_functional(dsn_env):
    """Part B, option B: the old repository stays importable and
    constructible (bootstrap/ResourceManager write sink), emitting a
    DeprecationWarning that points at the new adapter."""
    with pytest.warns(DeprecationWarning, match="PostgresResourceDefinitionRepository"):
        repo = PostgreSQLResourceRepository(lambda: None)

    # The legacy write-sink API surface is intact.
    for method in ("initialize", "add", "get", "require", "list", "update", "delete"):
        assert hasattr(repo, method), method


# -- Test 3: single source — one reconciliation service, repository-fed --------------


def test_startup_composition_has_exactly_one_repository_fed_reconciliation():
    repo_root = Path(__file__).resolve().parents[2]
    source = (repo_root / "app" / "main.py").read_text(encoding="utf-8")

    # Exactly one RuntimeReconciliationService construction in the app...
    assert source.count("RuntimeReconciliationService(") == 1
    # ...fed by the injected persistence repository (never a plain
    # definitions list, never a second construction)...
    assert "reconciliation_repository" in source
    assert not re.search(
        r"RuntimeReconciliationService\(\s*\[", source
    )
    # ...and the bootstrap service's read stays on the write-side
    # repository.  `ResourceRepositoryDefinitionSource(sink)` appears
    # exactly twice: the bootstrap diff read, and the memory fallback
    # INSIDE the single reconciliation construction (postgres never
    # takes that branch — it hands in the protocol repository).
    assert source.count("ResourceRepositoryDefinitionSource(sink)") == 2
    assert re.search(
        r"reconciliation_repository\s+if\s+reconciliation_repository"
        r"\s+is\s+not\s+None",
        source,
    )


def test_runtime_reconciliation_service_never_takes_a_definitions_list():
    """The service signature is repository-in only (Part A requirement:
    no RuntimeReconciliationService(definitions=config))."""
    from core.runtime_reconciliation import RuntimeReconciliationService

    params = inspect.signature(
        RuntimeReconciliationService.__init__
    ).parameters
    assert "definition_repository" in params
    for name, param in params.items():
        assert not re.search(r"definitions|list", name), name


# -- Functional wiring E2E (opt-in, REAL PG) ------------------------------------------
# The postgres create_app path needs a live database; without the env DSN
# this coverage is reported as an environment limitation.

REAL_DSN = __import__("os").environ.get("GEMINI_GATEWAY_TEST_DATABASE_URL")

if REAL_DSN is not None:
    import asyncio

    import os as _os

    if _os.name == "nt":
        import asyncio as _asyncio

        _asyncio.set_event_loop_policy(
            _asyncio.WindowsSelectorEventLoopPolicy()
        )

    from fastapi.testclient import TestClient

    from app.main import create_app
    from core.repositories.postgres import (
        PostgresResourceDefinitionRepository,
    )

    async def _make_e2e_database() -> str:
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        params = conninfo_to_dict(REAL_DSN)
        e2e_dbname = f"{params.get('dbname', 'postgres')}_r7_e2e"
        params["dbname"] = "postgres"
        conn = await psycopg.AsyncConnection.connect(
            make_conninfo(**params), autocommit=True
        )
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

    def test_postgres_create_app_wires_the_protocol_repository(
        monkeypatch,
    ):
        e2e_dsn = asyncio.run(_make_e2e_database())
        monkeypatch.setenv("ADMIN_TOKEN", "test-token")
        monkeypatch.setenv("GEMINI_GATEWAY_DATABASE_URL", e2e_dsn)

        app = create_app(
            config={
                "providers": {
                    "antigravity": {
                        "enabled": True,
                        "resources": [
                            {"id": "r1", "project_id": "p-seed"}
                        ],
                    }
                },
                "resource_store": {"backend": "postgres"},
                "resource_bootstrap": {"enabled": True, "mode": "import"},
            },
            config_path=Path("nonexistent-config.yaml"),
        )

        # The source of record on app.state is the protocol adapter...
        assert isinstance(
            app.state.resource_definition_repository,
            PostgresResourceDefinitionRepository,
        )
        # ...and the runtime was reconciled from it (seed resource live).
        pool = app.state.scheduler.pools["antigravity"]
        assert [r.id for r in pool.resources] == ["r1"]
        snapshot = app.state.runtime_snapshot
        assert snapshot.source_count == 1
