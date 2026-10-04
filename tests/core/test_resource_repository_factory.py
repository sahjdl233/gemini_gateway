"""Resource store factory tests (DB-RESOURCE-011).

Covers backend dispatch for the durable sink (Part A) and the
definition-source factory (Part C):

* memory is the default — no database, no config section required;
* postgres requires GEMINI_GATEWAY_DATABASE_URL (fail-closed when
  missing; constructing the repository never connects);
* unknown backends and malformed sections fail closed;
* the definition source of record dispatches on the same backend, while
  the bootstrap incoming seed is ALWAYS config-backed.
"""

from __future__ import annotations

import pytest

from core.resource_definition_repository import (
    ResourceRepositoryDefinitionSource,
)
from core.resource_definition_repository_factory import (
    create_config_definition_source,
    create_resource_definition_repository,
)
from core.resource_definition_loader import ConfigResourceDefinitionRepository
from core.resource_postgres import PostgreSQLResourceRepository
from core.resource_repository_factory import (
    ResourceStoreConfigurationError,
    create_resource_repository,
    resource_store_backend,
)
from core.resource_repository_memory import MemoryResourceRepository

DSN = "postgresql://gw:gwpass@localhost:15432/gw_resources"


@pytest.fixture
def dsn_env(monkeypatch):
    monkeypatch.setenv("GEMINI_GATEWAY_DATABASE_URL", DSN)
    return DSN


# -- Part A: sink factory ----------------------------------------------------------


def test_memory_is_the_default_backend():
    assert resource_store_backend({}) == "memory"
    assert resource_store_backend({"resource_store": None}) == "memory"
    repo = create_resource_repository({})
    assert type(repo) is MemoryResourceRepository


def test_explicit_memory_backend():
    repo = create_resource_repository({"resource_store": {"backend": "memory"}})
    assert type(repo) is MemoryResourceRepository


def test_postgres_backend_builds_pg_repository(dsn_env):
    repo = create_resource_repository({"resource_store": {"backend": "postgres"}})
    assert type(repo) is PostgreSQLResourceRepository
    # Constructing never connects — psycopg connects lazily per operation.


def test_postgres_without_dsn_fails_closed(monkeypatch):
    monkeypatch.delenv("GEMINI_GATEWAY_DATABASE_URL", raising=False)
    with pytest.raises(ResourceStoreConfigurationError, match="DATABASE_URL"):
        create_resource_repository({"resource_store": {"backend": "postgres"}})


def test_invalid_backend_fails_closed():
    with pytest.raises(ResourceStoreConfigurationError, match="backend"):
        create_resource_repository(
            {"resource_store": {"backend": "sqlite"}}
        )


def test_malformed_resource_store_section_fails_closed():
    with pytest.raises(ResourceStoreConfigurationError, match="mapping"):
        create_resource_repository({"resource_store": ["postgres"]})


def test_factory_never_connects_or_bootstraps():
    """The factory only constructs: its imports pull in no bootstrap
    service and no scheduler — construction, connection and execution
    stay separate concerns."""
    import ast
    import inspect

    import core.resource_repository_factory as module

    tree = ast.parse(inspect.getsource(module))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
    for forbidden in ("bootstrap", "Scheduler", "yaml"):
        assert not any(forbidden.lower() in name.lower() for name in imported), (
            forbidden,
            imported,
        )


# -- Part C: definition-source dispatch ----------------------------------------------


def config_with_resources():
    return {
        "providers": {
            "antigravity": {"resources": [{"id": "r1", "project_id": "p"}]}
        }
    }


def test_definition_source_memory_backend_unchanged():
    repo = create_resource_definition_repository(config_with_resources())
    assert type(repo) is ConfigResourceDefinitionRepository


def test_definition_source_postgres_backend_is_durable_adapter(dsn_env):
    """CONFIG/R-7 cutover: the postgres source of record is the new
    persistence-layer adapter (ResourceDefinitionRepository protocol),
    not the deprecated PostgreSQLResourceRepository read adapter."""
    from core.repositories import ResourceDefinitionRepository
    from core.repositories.postgres import (
        PostgresResourceDefinitionRepository,
    )

    repo = create_resource_definition_repository(
        {"resource_store": {"backend": "postgres"}}
    )
    assert type(repo) is PostgresResourceDefinitionRepository
    assert isinstance(repo, ResourceDefinitionRepository)
    assert not isinstance(repo, ResourceRepositoryDefinitionSource)


def test_no_postgres_resource_definition_repository_class_exists():
    """Part C: the read side is reached ONLY through the adapter — there
    is deliberately no separate PG definition-repository class."""
    import core.resource_postgres as pg_module
    import core.resource_definition_repository as repo_module

    assert not hasattr(pg_module, "list_definitions")
    assert not hasattr(repo_module, "PostgresResourceDefinitionRepository")
    assert not hasattr(repo_module, "PostgreSQLResourceDefinitionRepository")


async def test_bootstrap_incoming_is_always_config_backed(dsn_env):
    """Regardless of backend, the bootstrap incoming seed parses the
    config (YAML → store import semantics, ADR-002 §2)."""
    repo = create_config_definition_source(
        {
            **config_with_resources(),
            "resource_store": {"backend": "postgres"},
        }
    )
    assert type(repo) is ConfigResourceDefinitionRepository
    definitions = await repo.list_definitions()
    assert [(d.provider, d.id) for d in definitions] == [("antigravity", "r1")]
