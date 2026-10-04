"""Resource bootstrap startup wiring tests (DB-RESOURCE-006).

Covers the composition chain without FastAPI:

    config
      → repository factory          (core.resource_definition_repository_factory)
      → ResourceBootstrapService    (003-1/005 constructor)
      → plan (CHECK) / apply (IMPORT/OVERWRITE → sink)

Verifies the architectural rules:

* the factory returns Config- or Memory-backed repositories, never
  runtime Resources, and never writes anything;
* the bootstrap service does NOT accept a config mapping — composition
  into a service is the startup layer's job (the factory's);
* CHECK produces a plan with zero sink writes; IMPORT applies via
  sink.add; OVERWRITE additionally applies via sink.update;
* the sink records every write so the startup layer (and tests) can
  observe what was applied.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from core.resource_bootstrap import (
    BootstrapMode,
    ResourceBootstrapError,
    ResourceBootstrapService,
)
from core.resource_definition import AntigravityResourceDefinition
from core.resource_definition_loader import ConfigResourceDefinitionRepository
from core.resource_definition_repository import (
    MemoryResourceDefinitionRepository,
    ResourceDefinitionRepository,
    ResourceRepositoryDefinitionSource,
)
from core.resource_definition_repository_factory import (
    create_resource_definition_repository,
    resource_bootstrap_settings,
)
from core.resource_repository import (
    DuplicateResourceDefinitionError,
    ResourceRepository,
    UnknownResourceDefinitionError,
)
from core.resource_repository_memory import MemoryResourceRepository


def config_with_resources() -> Dict[str, Any]:
    return {
        "providers": {
            "antigravity": {
                "enabled": True,
                "resources": [
                    {"id": "r1", "project_id": "p1"},
                    {"id": "r2", "enabled": False, "credential_id": "c2"},
                ],
            }
        }
    }


# -- repository factory ----------------------------------------------------------


def test_factory_returns_config_repository_for_providers():
    repo = create_resource_definition_repository(config_with_resources())
    assert type(repo) is ConfigResourceDefinitionRepository


def test_factory_returns_memory_repository_without_providers():
    for config in ({}, {"providers": None}, {"providers": {}}):
        repo = create_resource_definition_repository(config)
        assert type(repo) is MemoryResourceDefinitionRepository


async def test_factory_created_repository_serves_sorted_dtos():
    repo = create_resource_definition_repository(config_with_resources())
    defs = await repo.list_definitions()
    assert [(d.provider, d.id, d.enabled) for d in defs] == [
        ("antigravity", "r1", True),
        ("antigravity", "r2", False),
    ]


def test_factory_is_pure_composition():
    """The factory must not create runtime Resources or touch
    bootstrap/scheduler: it is a Mapping in, repository out function."""
    import inspect

    import core.resource_definition_repository_factory as factory

    source = inspect.getsource(factory)
    assert "import yaml" not in source
    assert "ResourceBootstrapService" not in source.replace(
        "from core.resource_bootstrap import BootstrapMode, ResourceBootstrapError",
        "",
    )
    assert "Scheduler" not in source
    assert "build_runtime" not in source


# -- bootstrap behavior settings -------------------------------------------------


def test_settings_default_disabled_without_section():
    enabled, mode = resource_bootstrap_settings({})
    assert enabled is False
    assert mode is BootstrapMode.CHECK  # placeholder, never executed


def test_settings_parse_enabled_modes():
    """CONFIG-003: only check/import are legal persistent startup modes."""
    for raw in ("check", "import"):
        enabled, mode = resource_bootstrap_settings(
            {"resource_bootstrap": {"enabled": True, "mode": raw}}
        )
        assert enabled is True
        assert mode is BootstrapMode(raw)


def test_bootstrap_overwrite_mode_rejected():
    """CONFIG-001-ADR §2/§3: `overwrite` is a mutation operation, not a
    lifecycle policy — as a persistent startup config it is rejected
    outright (fail-closed), pointing operators at the explicit one-shot
    import command.  The service-level OVERWRITE capability itself stays
    available to that tooling (tests/core/test_resource_bootstrap.py)."""
    with pytest.raises(ResourceBootstrapError) as exc_info:
        resource_bootstrap_settings(
            {"resource_bootstrap": {"enabled": True, "mode": "overwrite"}}
        )
    message = str(exc_info.value)
    assert "no longer supported as a startup policy" in message
    assert "explicit resource import command" in message


def test_settings_reject_invalid_values():
    with pytest.raises(ResourceBootstrapError, match="mode"):
        resource_bootstrap_settings(
            {"resource_bootstrap": {"enabled": True, "mode": "required"}}
        )
    with pytest.raises(ResourceBootstrapError, match="enabled"):
        resource_bootstrap_settings(
            {"resource_bootstrap": {"enabled": "yes"}}
        )
    with pytest.raises(ResourceBootstrapError, match="mapping"):
        resource_bootstrap_settings({"resource_bootstrap": ["check"]})


# -- the service does not accept config --------------------------------------------


async def test_bootstrap_service_rejects_config_as_source():
    """Passing a raw config mapping where a repository belongs fails at
    plan time — only the startup composition turns config into a
    repository."""
    config = config_with_resources()
    service = ResourceBootstrapService(config)  # type: ignore[arg-type]
    with pytest.raises(AttributeError, match="list_definitions"):
        await service.run([], BootstrapMode.CHECK)


# -- startup composition: plan / apply against a spy sink --------------------------


class SpySink(ResourceRepository):
    """Memory sink that records which write methods were invoked."""

    def __init__(self) -> None:
        self._inner = MemoryResourceRepository()
        self.added: List[str] = []
        self.updated: List[str] = []
        self.deleted: List[str] = []

    async def add(self, definition):
        self.added.append(f"{definition.provider}/{definition.id}")
        return await self._inner.add(definition)

    async def get(self, provider, resource_id):
        return await self._inner.get(provider, resource_id)

    async def require(self, provider, resource_id):
        return await self._inner.require(provider, resource_id)

    async def list(self, *, provider: Optional[str] = None):
        return await self._inner.list(provider=provider)

    async def update(self, definition):
        self.updated.append(f"{definition.provider}/{definition.id}")
        return await self._inner.update(definition)

    async def delete(self, provider, resource_id):
        self.deleted.append(f"{provider}/{resource_id}")
        return await self._inner.delete(provider, resource_id)


def compose(config: Dict[str, Any], sink: ResourceRepository):
    """Exactly the startup composition from app/main.py, testable:
    factory → source-adapter + sink → service; incoming definitions come
    from the config-backed repository via its read protocol."""
    definition_repo = create_resource_definition_repository(config)
    service = ResourceBootstrapService(
        ResourceRepositoryDefinitionSource(sink),
        sink=sink,
    )
    return definition_repo, service


async def run_startup(config: Dict[str, Any], sink: ResourceRepository):
    definition_repo, service = compose(config, sink)
    incoming = await definition_repo.list_definitions()
    _enabled, mode = resource_bootstrap_settings(config)
    return await service.run(incoming, mode)


async def test_startup_check_produces_plan_without_sink_writes():
    sink = SpySink()
    result = await run_startup(
        {
            **config_with_resources(),
            "resource_bootstrap": {"enabled": True, "mode": "check"},
        },
        sink,
    )
    # Plan generated: both config definitions are new to the empty sink.
    assert [r.key for r in result.added] == [
        ("antigravity", "r1"),
        ("antigravity", "r2"),
    ]
    assert result.mode is BootstrapMode.CHECK
    assert not result.has_conflicts
    # CHECK never touches the sink.
    assert sink.added == [] and sink.updated == [] and sink.deleted == []


async def test_startup_import_applies_via_sink_add():
    sink = SpySink()
    result = await run_startup(
        {
            **config_with_resources(),
            "resource_bootstrap": {"enabled": True, "mode": "import"},
        },
        sink,
    )
    assert sorted(sink.added) == ["antigravity/r1", "antigravity/r2"]
    assert sink.updated == []
    stored = await sink.get("antigravity", "r2")
    assert stored.enabled is False
    assert stored.credential_id == "c2"


async def test_startup_import_is_idempotent_across_restarts():
    sink = SpySink()
    config = {
        **config_with_resources(),
        "resource_bootstrap": {"enabled": True, "mode": "import"},
    }
    await run_startup(config, sink)
    # Simulated restart against an already-populated sink: everything is
    # unchanged, zero further writes.
    result = await run_startup(config, sink)
    assert [r.key for r in result.unchanged] == [
        ("antigravity", "r1"),
        ("antigravity", "r2"),
    ]
    assert sink.added == ["antigravity/r1", "antigravity/r2"]  # no repeats
    assert sink.updated == []


async def test_startup_overwrite_mode_is_rejected_fail_closed():
    """CONFIG-003: the startup composition refuses `mode: overwrite`
    outright — no plan, no sink write — even when the sink already
    holds conflicting rows.  (Service-level OVERWRITE semantics remain
    covered in tests/core/test_resource_bootstrap.py, reserved for the
    one-shot import command.)"""
    sink = SpySink()
    await run_startup(
        {
            "providers": {"antigravity": {"resources": [
                {"id": "r1", "project_id": "p-old"},
            ]}},
            "resource_bootstrap": {"enabled": True, "mode": "import"},
        },
        sink,
    )
    assert sink.added == ["antigravity/r1"]

    with pytest.raises(
        ResourceBootstrapError,
        match="no longer supported as a startup policy",
    ):
        await run_startup(
            {
                "providers": {"antigravity": {"resources": [
                    {"id": "r1", "project_id": "p-new", "enabled": False},
                ]}},
                "resource_bootstrap": {
                    "enabled": True, "mode": "overwrite"
                },
            },
            sink,
        )
    # Fail-closed: the sink holds the original import, nothing updated.
    assert sink.updated == []
    stored = await sink.get("antigravity", "r1")
    assert stored.project_id == "p-old"


async def test_startup_missing_providers_is_a_noop_plan():
    sink = SpySink()
    result = await run_startup(
        {
            "resource_bootstrap": {"enabled": True, "mode": "import"},
        },
        sink,
    )
    assert result.added == [] and result.unchanged == []
    assert result.conflicts == [] and result.db_only == []
    assert sink.added == []


# -- disabled by default: pre-006 behavior preserved ---------------------------------


def test_default_config_has_no_bootstrap_section():
    from config.loader import default_config

    enabled, _mode = resource_bootstrap_settings(default_config())
    assert enabled is False
