"""CONFIG/R-2-C: resource reconciliation triggers ModelRegistry invalidation.

The control-plane chain under contract:

    definition mutation (create/update/delete/enable-disable)
        → repository write → runtime reconcile SUCCESS
        → ModelRegistry.invalidate()

Failure semantics: a reconcile failure is rolled back — the discovery
cache must NOT be told that definitions took effect.  The dependency is
optional: a ResourceManager without a model_registry works unchanged.

Boundary (per docs/CONFIG-R2B-MODEL-REGISTRY-INVALIDATION.md): only the
manager calls invalidate, strictly after success; the repository, the
RuntimeReconciliationService and the scheduler know nothing about it.
"""

from __future__ import annotations

import asyncio

import pytest

from app.main import create_app


class SpyRegistry:
    """Duck-typed ModelRegistry stand-in recording invalidate() calls."""

    def __init__(self) -> None:
        self.invalidate_calls = 0

    def invalidate(self) -> None:
        self.invalidate_calls += 1


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-token")
    return tmp_path / "config.yaml"


def make_app(config_path):
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


def _spy_manager(app) -> SpyRegistry:
    """Swap the wired registry for a spy; return the spy."""
    spy = SpyRegistry()
    app.state.resource_manager._model_registry = spy
    return spy


# -- Case A: successful mutation → invalidate called ---------------------------------


async def test_successful_mutation_invalidates_registry(env):
    app = await asyncio.to_thread(make_app, env)
    spy = _spy_manager(app)
    manager = app.state.resource_manager

    await manager.update_resource(
        "antigravity", "r1", {"project_id": "p-edited"}
    )
    assert spy.invalidate_calls == 1

    # Every successful definition mutation keeps the chain: create,
    # enable/disable (→ update), delete.
    await manager.create_resource(
        {"id": "r2", "project_id": "p2"}, provider_id="antigravity"
    )
    assert spy.invalidate_calls == 2
    await manager.set_enabled("antigravity", "r2", False)
    assert spy.invalidate_calls == 3
    await manager.delete_resource("antigravity", "r2")
    assert spy.invalidate_calls == 4


async def test_legacy_mutation_invalidates_registry(env):
    """The legacy (bootstrap-disabled) path also mutates definitions and
    therefore invalidates after success."""
    app = await asyncio.to_thread(
        create_app,
        {
            "providers": {
                "antigravity": {
                    "enabled": True,
                    "resources": [{"id": "r1", "project_id": "p"}],
                }
            }
        },
        config_path=env,
    )
    spy = _spy_manager(app)
    manager = app.state.resource_manager

    await manager.update_resource("antigravity", "r1", {"project_id": "p2"})
    assert spy.invalidate_calls == 1
    await manager.create_resource(
        {"id": "r2", "project_id": "p2"}, provider_id="antigravity"
    )
    assert spy.invalidate_calls == 2
    await manager.delete_resource("antigravity", "r2")
    assert spy.invalidate_calls == 3


# -- Case B: reconcile failure → invalidate NOT called --------------------------------


async def test_failed_reconcile_does_not_invalidate(env, monkeypatch):
    """A rolled-back mutation never reached the runtime — telling the
    discovery cache that definitions changed would be a lie."""
    app = await asyncio.to_thread(make_app, env)
    spy = _spy_manager(app)
    manager = app.state.resource_manager
    pool = app.state.scheduler.pools["antigravity"]

    async def boom(new_resources):
        raise RuntimeError("reconcile boom")

    monkeypatch.setattr(pool, "reconcile_resources", boom)
    with pytest.raises(Exception, match="reconcile boom"):
        await manager.update_resource(
            "antigravity", "r1", {"project_id": "p-edited"}
        )
    assert spy.invalidate_calls == 0

    # And the failed write was rolled back: the store kept the old value.
    stored = await app.state.resource_sink.get("antigravity", "r1")
    assert stored.project_id == "p-seed"


# -- Case C: no ModelRegistry → fully backward compatible ------------------------------


async def test_manager_without_registry_works(env):
    """model_registry defaults to None: every mutation path no-ops the
    invalidation instead of raising."""
    app = await asyncio.to_thread(make_app, env)
    manager = app.state.resource_manager
    assert manager._model_registry is not None  # wired by create_app

    # Detach: the plain-constructor situation.
    manager._model_registry = None
    await manager.update_resource(
        "antigravity", "r1", {"project_id": "p-edited"}
    )
    await manager.create_resource(
        {"id": "r2", "project_id": "p2"}, provider_id="antigravity"
    )
    await manager.delete_resource("antigravity", "r2")
    stored = await app.state.resource_sink.get("antigravity", "r1")
    assert stored.project_id == "p-edited"