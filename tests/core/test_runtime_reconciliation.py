"""RuntimeReconciliationService tests (DB-RESOURCE-008, Part E).

The service is exercised purely through its contracts — a fake
definition repository (the read-only 004 Protocol) and a fake/real
runtime builder — with no FastAPI, app.state, Scheduler, YAML or
PostgreSQL anywhere.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

import pytest

from core.resource_definition import AntigravityResourceDefinition
from core.runtime_reconciliation import RuntimeReconciliationService
from core.runtime_resource_factory import registry_runtime_builder


class FakeDefinitionRepository:
    """Minimal read-only definition source; backing list is mutable so
    tests can prove the service does not cache."""

    def __init__(self, definitions: Optional[List] = None) -> None:
        self.definitions = list(definitions or [])

    async def list_definitions(self) -> List:
        return list(self.definitions)

    async def get_definition(self, provider: str, id: str):
        for definition in self.definitions:
            if definition.provider == provider and definition.id == id:
                return definition
        return None


def antigravity_definition(rid: str = "r1", project_id: str = "p1"):
    return AntigravityResourceDefinition(
        id=rid, enabled=True, credential_id=None, project_id=project_id
    )


def fake_builder(definitions) -> Dict[str, List]:
    """Deterministic fake conversion: provider -> [Resource-like dicts]."""
    grouped: Dict[str, List] = {}
    for definition in definitions:
        grouped.setdefault(definition.provider, []).append(
            {"provider": definition.provider, "id": definition.id}
        )
    return grouped


def fixed_clock():
    class _Clock:
        calls = 0

        def __call__(self):
            _Clock.calls += 1
            return datetime(2026, 10, 3, tzinfo=timezone.utc)

    return _Clock()


# -- 1. service does not depend on app -------------------------------------------


def test_service_source_has_no_app_or_scheduler_dependencies():
    import inspect

    import core.runtime_reconciliation as module

    import re

    source = inspect.getsource(module)
    # No whole-word references to the forbidden layers.
    for forbidden in ("app", "FastAPI", "Scheduler", "yaml", "psycopg",
                      "app.state"):
        assert not re.search(
            rf"{re.escape(forbidden)}", source
        ), forbidden


async def test_fake_repository_reconciles_to_snapshot():
    service = RuntimeReconciliationService(
        FakeDefinitionRepository([antigravity_definition("r1")]),
        runtime_builder=fake_builder,
        clock=fixed_clock(),
    )
    snapshot = await service.reconcile()
    assert snapshot.resources == [
        {"provider": "antigravity", "id": "r1"}
    ]
    assert snapshot.source_count == 1
    assert snapshot.generated_at == datetime(2026, 10, 3, tzinfo=timezone.utc)


# -- 2. definition -> runtime ------------------------------------------------------


async def test_definition_converts_to_runtime_resource():
    registry = _fresh_registry()
    service = RuntimeReconciliationService(
        FakeDefinitionRepository([antigravity_definition("r1", "p-x")]),
        runtime_builder=registry_runtime_builder(registry),
    )
    snapshot = await service.reconcile()
    assert snapshot.source_count == 1
    assert [r.id for r in snapshot.resources] == ["r1"]
    resource = snapshot.resources[0]
    assert resource.provider == "antigravity"
    assert resource.project_id == "p-x"
    assert resource.enabled is True
    assert snapshot.resources_by_provider["antigravity"] == snapshot.resources


def _fresh_registry():
    from app.bootstrap import register_builtin_providers
    from core.provider_registry import ProviderRegistry

    registry = ProviderRegistry()
    register_builtin_providers(registry)
    return registry


# -- 3. source replacement: no caching ----------------------------------------------


async def test_source_changes_are_reflected_in_next_reconcile():
    """Same service, mutated source: every reconcile re-reads the
    repository — the service never caches the source or the snapshot."""
    repo = FakeDefinitionRepository([antigravity_definition("r1")])
    service = RuntimeReconciliationService(
        repo, runtime_builder=fake_builder, clock=fixed_clock()
    )
    first = await service.reconcile()
    assert [r["id"] for r in first.resources] == ["r1"]

    repo.definitions.append(antigravity_definition("r2"))
    second = await service.reconcile()
    assert [r["id"] for r in second.resources] == ["r1", "r2"]
    assert second.source_count == 2

    repo.definitions = []
    third = await service.reconcile()
    assert third.resources == []
    assert third.source_count == 0


async def test_distinct_sources_yield_distinct_snapshots():
    repo_a = FakeDefinitionRepository([antigravity_definition("a-only")])
    repo_b = FakeDefinitionRepository([antigravity_definition("b-only")])
    clock = fixed_clock()
    service_a = RuntimeReconciliationService(
        repo_a, runtime_builder=fake_builder, clock=clock
    )
    service_b = RuntimeReconciliationService(
        repo_b, runtime_builder=fake_builder, clock=clock
    )
    snapshot_a = await service_a.reconcile()
    snapshot_b = await service_b.reconcile()
    assert snapshot_a.resources != snapshot_b.resources
    assert snapshot_a.source_count == snapshot_b.source_count == 1


# -- 4. empty definitions --------------------------------------------------------


async def test_empty_source_yields_empty_snapshot_not_error():
    service = RuntimeReconciliationService(
        FakeDefinitionRepository([]),
        runtime_builder=fake_builder,
        clock=fixed_clock(),
    )
    snapshot = await service.reconcile()
    assert snapshot.resources == []
    assert snapshot.source_count == 0
    assert snapshot.resources_by_provider == {}


# -- 5. runtime factory failure propagates -----------------------------------------


async def test_builder_failure_propagates_unchanged():
    class ExplodingBuilder:
        def __call__(self, definitions):
            raise RuntimeError("factory exploded")

    service = RuntimeReconciliationService(
        FakeDefinitionRepository([antigravity_definition("r1")]),
        runtime_builder=ExplodingBuilder(),
    )
    with pytest.raises(RuntimeError, match="factory exploded"):
        await service.reconcile()


async def test_unknown_provider_failure_propagates():
    """A definition naming an unregistered provider surfaces the
    registry's own UnknownProviderError — never swallowed."""
    from core.provider_registry import UnknownProviderError

    from core.provider_registry import ProviderRegistry

    # No provider is registered in this bare registry.
    empty_registry = ProviderRegistry()
    service = RuntimeReconciliationService(
        FakeDefinitionRepository([antigravity_definition("r1")]),
        runtime_builder=registry_runtime_builder(empty_registry),
    )
    with pytest.raises(UnknownProviderError):
        await service.reconcile()


async def test_repository_failure_propagates():
    class FailingRepository:
        async def list_definitions(self):
            raise RuntimeError("source down")

    service = RuntimeReconciliationService(
        FailingRepository(), runtime_builder=fake_builder
    )
    with pytest.raises(RuntimeError, match="source down"):
        await service.reconcile()
