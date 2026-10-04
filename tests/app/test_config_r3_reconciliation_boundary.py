"""CONFIG/R-3: startup and runtime reconciliation share one boundary.

Frozen architecture (audit-backed):

    startup:   config seed ──ResourceBootstrapService──→ repository
                                                    │
               RuntimeReconciliationService ←──────┘ (composed in main)
                                                    │
               registry_runtime_builder → ProviderRegistry.create_resources
                                                    │
               build_runtime wires pools from the snapshot
                                                    │
               ModelRegistry — lazy: no discovery until first query

    runtime:   Admin mutation → ResourceManager → SAME
               RuntimeReconciliationService → pool reconcile → invalidate

Guarantees pinned here:

* there is exactly ONE definition→runtime conversion vector
  (ProviderRegistry.create_resources, called from
  core.runtime_resource_factory and the provider factories) — no module
  may construct runtime Resources from config/DB rows directly;
* startup performs no model discovery (lazy first query, Option A);
* a successful runtime mutation invalidates the model index (R-2-C).
"""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path

import pytest

from app.main import create_app


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-token")
    return tmp_path / "config.yaml"


def _make_app(config_path):
    return create_app(
        config={
            "providers": {
                "antigravity": {
                    "enabled": True,
                    "resources": [{"id": "r1", "project_id": "p-seed"}],
                }
            },
            "resource_bootstrap": {"enabled": True, "mode": "import"},
        },
        config_path=config_path,
    )


# -- Case A: the startup path lands in the same runtime boundary ---------------------


def test_startup_builds_runtime_through_snapshot_not_a_second_path(env):
    """Bootstrap-enabled startup: the seed is imported into the
    repository, the runtime snapshot is reconciled from the repository,
    and the pools are seeded from that snapshot — one conversion vector,
    no second construction path."""
    app = _make_app(env)

    # The snapshot was reconciled from the repository...
    snapshot = app.state.runtime_snapshot
    assert snapshot.source_count == 1
    antigravity = snapshot.resources_by_provider["antigravity"]
    assert [r.id for r in antigravity] == ["r1"]
    assert antigravity[0].project_id == "p-seed"

    # ...and the pools hold exactly those reconciled resources.
    pool = app.state.scheduler.pools["antigravity"]
    assert pool.resources is not None
    assert [r.id for r in pool.resources] == ["r1"]
    assert pool.resources[0].project_id == "p-seed"

    # The manager is repository-backed and shares the boundary.
    manager = app.state.resource_manager
    assert manager._repository_backed is True


def test_startup_model_registry_is_lazy_option_a(env):
    """Frozen behaviour (Option A): startup performs NO discovery — the
    model index is built lazily on the first query.  If anyone adds a
    startup refresh call, this pins the change for review."""
    app = _make_app(env)
    registry = app.state.scheduler.model_registry
    assert registry.last_refresh is None


# -- Case B: runtime mutation flows through the same boundary ------------------------


class SpyRegistry:
    def __init__(self) -> None:
        self.invalidate_calls = 0

    def invalidate(self) -> None:
        self.invalidate_calls += 1


def test_runtime_mutation_reconciles_and_invalidates(env):
    app = _make_app(env)
    spy = SpyRegistry()
    app.state.resource_manager._model_registry = spy

    async def mutate():
        manager = app.state.resource_manager
        await manager.update_resource(
            "antigravity", "r1", {"project_id": "p-edited"}
        )

    asyncio.run(mutate())

    # The mutation went through reconcile (pool updated)...
    pool = app.state.scheduler.pools["antigravity"]
    assert pool.resources[0].project_id == "p-edited"
    # ...and the discovery cache was invalidated — same boundary as
    # startup, not a second mechanism (tests/app/test_r2c_* covers the
    # full matrix; this pins the chain within the boundary picture).
    assert spy.invalidate_calls == 1


# -- Case C: no bypass — runtime Resources are only ever created via the registry ----

#: Modules that sit inside the startup/reconciliation boundary.  None of
#: them may construct runtime Resources directly; creation belongs to
#: ProviderRegistry.create_resources (provider factories) — everything
#: else converts definitions into payloads first.
_BOUNDARY_MODULES = (
    "app/main.py",
    "core/resource_bootstrap.py",
    "core/runtime_reconciliation.py",
    "core/runtime_resource_factory.py",
    "core/resource_repository.py",
    "core/resource_repository_memory.py",
    "core/resource_postgres.py",
    "core/pool.py",
    "core/scheduler.py",
)

_CONSTRUCTOR_PATTERN = re.compile(
    r"\b(AntigravityResource|GeminiCliResource|FirebaseResource|"
    r"AnonymousVertexResource|Resource)\("
)


def test_no_module_inside_the_boundary_constructs_runtime_resources():
    repo_root = Path(__file__).resolve().parents[2]
    for rel in _BOUNDARY_MODULES:
        source = (repo_root / rel).read_text(encoding="utf-8")
        hits = _CONSTRUCTOR_PATTERN.findall(source)
        assert not hits, (
            f"{rel} constructs runtime Resources directly ({hits}) — "
            "definitions must reach runtime via ProviderRegistry."
            "create_resources only"
        )


def test_the_only_legacy_resource_construction_is_the_sanctioned_one():
    """`AntigravityResource(**values)` in the manager is the documented
    Part C legacy path (bootstrap disabled; YAML remains that
    deployment's store).  If a second in-place construction appears on
    the repository path, this fails."""
    repo_root = Path(__file__).resolve().parents[2]
    source = (repo_root / "app/management.py").read_text(encoding="utf-8")
    legacy_sites = re.findall(r"AntigravityResource\(\*\*values\)", source)
    assert len(legacy_sites) == 1
    # And it lives inside the legacy create helper.
    legacy_create = source.split("async def _legacy_create")[1].split(
        "async def _legacy_update"
    )[0]
    assert "AntigravityResource(**values)" in legacy_create


def test_runtime_resource_factory_routes_everything_through_the_registry():
    """The conversion module must not gain a second creation vector: both
    the DTO builder and the legacy config source delegate to
    ``registry.create_resources``."""
    repo_root = Path(__file__).resolve().parents[2]
    source = (
        repo_root / "core/runtime_resource_factory.py"
    ).read_text(encoding="utf-8")
    assert source.count("registry.create_resources") == 2
    assert _CONSTRUCTOR_PATTERN.search(source) is None
