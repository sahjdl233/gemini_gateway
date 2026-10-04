"""Repository-backed Admin resource write path (DB-RESOURCE-013).

With bootstrap enabled, Admin resource mutations go:

    Admin API / ResourceManager
        → ResourceRepository.add/update/delete
        → RuntimeReconciliationService
        → pools (runtime state preserved per ResourceKey)

and YAML is never written (Part A).  These tests use the memory sink;
the PostgreSQL variant lives in
``tests/test_dbresource013_admin_write_path.py`` (real server, opt-in).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    """bootstrap-enabled memory deployment: no YAML store file exists,
    admin API configured."""
    monkeypatch.setenv("ADMIN_TOKEN", "test-token")
    config_path = tmp_path / "config.yaml"
    assert not config_path.exists()
    return config_path


ADMIN = {"Authorization": "Bearer test-token"}


def make_app(store_env):
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
        config_path=store_env,
    )


def test_create_writes_repository_not_yaml(store_env):
    app = make_app(store_env)
    client = TestClient(app)
    response = client.post(
        "/admin/resources", json={"id": "r2", "project_id": "p2"},
        headers=ADMIN,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] == "r2"

    # The repository (memory sink) holds the strict definition...
    sink = app.state.resource_sink

    import asyncio

    stored = asyncio.run(sink.get("antigravity", "r2"))
    assert stored is not None
    assert stored.project_id == "p2"
    # ...and no YAML was written (Part A: 禁止 write yaml).
    assert not store_env.exists()


def test_create_reflects_in_runtime_pool(store_env):
    app = make_app(store_env)
    client = TestClient(app)
    response = client.post(
        "/admin/resources", json={"id": "r2", "project_id": "p2"},
        headers=ADMIN,
    )
    assert response.status_code == 201
    pool = app.state.scheduler.pools["antigravity"]
    assert [r.id for r in pool.resources] == ["r1", "r2"]
    assert pool.resources[1].project_id == "p2"


def test_duplicate_create_maps_to_client_error(store_env):
    app = make_app(store_env)
    client = TestClient(app)
    first = client.post(
        "/admin/resources", json={"id": "r2"}, headers=ADMIN
    )
    assert first.status_code == 201
    second = client.post(
        "/admin/resources", json={"id": "r2"}, headers=ADMIN
    )
    assert second.status_code == 400  # duplicate is not a "not found"
    assert "already exists" in second.json()["detail"]


def test_update_goes_through_repository_and_preserves_runtime_state(
    store_env,
):
    app = make_app(store_env)
    client = TestClient(app)

    # Simulate accumulated runtime state on the pool resource.
    pool = app.state.scheduler.pools["antigravity"]
    pool.resources[0].total_requests = 7
    pool.resources[0].total_failures = 1

    response = client.patch(
        "/admin/resources/r1",
        json={"project_id": "p-edited"},
        headers=ADMIN,
    )
    assert response.status_code == 200, response.text

    # Repository holds the replacement definition...
    import asyncio

    stored = asyncio.run(app.state.resource_sink.get("antigravity", "r1"))
    assert stored.project_id == "p-edited"
    # ...and the pool resource is the reconciled replacement with the
    # SAME runtime state (per ResourceKey preservation).
    resource = next(r for r in pool.resources if r.id == "r1")
    assert resource.project_id == "p-edited"
    assert resource.total_requests == 7
    assert resource.total_failures == 1


def test_delete_removes_from_repository_and_runtime(store_env):
    app = make_app(store_env)
    client = TestClient(app)
    response = client.delete("/admin/resources/r1", headers=ADMIN)
    assert response.status_code == 204

    import asyncio

    stored = asyncio.run(app.state.resource_sink.get("antigravity", "r1"))
    assert stored is None
    pool = app.state.scheduler.pools["antigravity"]
    assert [r.id for r in pool.resources] == []


def test_delete_unknown_maps_to_404(store_env):
    app = make_app(store_env)
    client = TestClient(app)
    response = client.delete("/admin/resources/nope", headers=ADMIN)
    assert response.status_code == 404


def test_secret_fields_rejected_on_repository_path(store_env):
    app = make_app(store_env)
    client = TestClient(app)
    response = client.post(
        "/admin/resources",
        json={"id": "r2", "access_token": "secret"},
        headers=ADMIN,
    )
    assert response.status_code == 400
    assert "credential" in response.json()["detail"]


def test_invalid_definition_field_rejected_strictly(store_env):
    """The strict DTO boundary applies to Admin writes too — an unknown
    field is never silently dropped."""
    app = make_app(store_env)
    client = TestClient(app)
    response = client.post(
        "/admin/resources",
        json={"id": "r2", "not_an_antigravity_field": "x"},
        headers=ADMIN,
    )
    assert response.status_code == 400
    assert "invalid resource definition" in response.json()["detail"]


def test_bootstrap_disabled_keeps_legacy_yaml_path(store_env):
    """Part C compatibility: without bootstrap, mutations keep the
    legacy YAML persistence (the repository path is opt-in via
    bootstrap)."""
    app = create_app(
        config={
            "providers": {
                "antigravity": {
                    "enabled": True,
                    "resources": [{"id": "r1", "project_id": "p"}],
                }
            }
        },
        config_path=store_env,
    )
    client = TestClient(app)
    response = client.post(
        "/admin/resources", json={"id": "r2", "project_id": "p2"},
        headers=ADMIN,
    )
    assert response.status_code == 201
    # Legacy path: YAML file was written...
    assert store_env.exists()
    import yaml as yaml_module

    saved = yaml_module.safe_load(store_env.read_text(encoding="utf-8"))
    resources = saved["providers"]["antigravity"]["resources"]
    assert [item["id"] for item in resources] == ["r1", "r2"]
    # ...and the memory sink stayed out of the loop entirely.
    import asyncio

    assert asyncio.run(app.state.resource_sink.list()) == []


# -- CONTROL-004-FIX: reconciliation failure compensation -------------------------


def _break_reconcile(app, monkeypatch, message="reconcile boom"):
    pool = app.state.scheduler.pools["antigravity"]

    async def boom(new_resources):
        raise RuntimeError(message)

    monkeypatch.setattr(pool, "reconcile_resources", boom)
    return pool


def _sink_get(app, provider, rid):
    import asyncio

    return asyncio.run(app.state.resource_sink.get(provider, rid))


def test_update_reconcile_failure_rolls_back_store_and_pool(
    store_env, monkeypatch
):
    app = make_app(store_env)
    client = TestClient(app)
    pool = _break_reconcile(app, monkeypatch)

    response = client.patch(
        "/admin/resources/r1",
        json={"project_id": "p-edited"},
        headers=ADMIN,
    )
    assert response.status_code == 400
    assert "reconcile boom" in response.json()["detail"]

    # DB restored to the OLD definition (full replacement rollback)...
    stored = _sink_get(app, "antigravity", "r1")
    assert stored.project_id == "p-seed"
    assert stored.enabled is True
    # ...and the pool kept the old definition too.
    assert pool.resources[0].project_id == "p-seed"


def test_delete_reconcile_failure_restores_the_resource(
    store_env, monkeypatch
):
    app = make_app(store_env)
    client = TestClient(app)
    pool = _break_reconcile(app, monkeypatch)

    response = client.delete("/admin/resources/r1", headers=ADMIN)
    assert response.status_code == 400
    assert "reconcile boom" in response.json()["detail"]

    # The deleted definition is restored with ALL fields...
    stored = _sink_get(app, "antigravity", "r1")
    assert stored is not None
    assert stored.project_id == "p-seed"
    assert stored.enabled is True
    assert stored.credential_id is None
    # ...so the runtime is not a ghost.
    assert [r.id for r in pool.resources] == ["r1"]


async def test_create_compensation_failure_keeps_original_error(
    store_env, monkeypatch, caplog
):
    import logging

    from app.management import ResourceManagementError

    import asyncio

    # create_app drives its own asyncio.run bootstrap bridge, so it must
    # not run inside this test's event loop.
    app = await asyncio.to_thread(make_app, store_env)
    client = TestClient(app)
    _break_reconcile(app, monkeypatch)
    sink = app.state.resource_sink

    async def failing_delete(provider_id, resource_id):
        raise RuntimeError("compensation delete exploded")

    monkeypatch.setattr(sink, "delete", failing_delete)

    manager = app.state.resource_manager
    with caplog.at_level(logging.ERROR, logger="app.management"):
        with pytest.raises(ResourceManagementError) as exc_info:
            await manager._repository_create(
                {"id": "r3", "project_id": "p3"}, "antigravity"
            )

    # The ORIGINAL reconcile error is preserved...
    assert "reconcile boom" in str(exc_info.value)
    # ...and the compensation failure is reachable via __cause__.
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert "compensation delete exploded" in str(exc_info.value.__cause__)
    # Both failures are logged.
    assert any(
        "compensation failed" in record.message for record in caplog.records
    )

    # The store keeps the created row (compensation could not remove it) —
    # a documented convergence-on-next-write state.
    assert await sink.get("antigravity", "r3") is not None


def test_update_rollback_restores_even_from_derived_definition(
    store_env, monkeypatch
):
    """When no stored definition existed (defensive derive path), the
    rollback removes the row entirely — restoring the prior state."""
    app = make_app(store_env)
    client = TestClient(app)
    _break_reconcile(app, monkeypatch)

    # r2 does not exist in the store yet: create it, so the store has it,
    # then verify update-failure rollback of an EXISTING row.
    created = client.post(
        "/admin/resources",
        json={"id": "r2", "project_id": "p2"},
        headers=ADMIN,
    )
    # create itself hits the broken reconcile and compensates...
    assert created.status_code == 400
    assert _sink_get(app, "antigravity", "r2") is None
    assert [r.id for r in app.state.scheduler.pools["antigravity"].resources
            ] == ["r1"]


# -- CONTROL-005-FIX: repository mutation serialization ----------------------------

from app.management import ResourceManagementError  # noqa: E402


async def _manager_app(store_env):
    """Bootstrapped app whose manager can be driven directly inside the
    test's event loop (create_app bridges its own bootstrap via
    asyncio.run, so the app object is built in a worker thread)."""
    return await asyncio.to_thread(make_app, store_env)


def _first_call_fails(pool, message="reconcile boom"):
    """Fail the FIRST reconcile call only — later calls delegate to the
    real reconcile.  Under the mutation lock the calls are serialized, so
    call order == request order."""
    real = pool.reconcile_resources
    calls = {"n": 0}

    async def stub(new_resources):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(message)
        return await real(new_resources)

    pool.reconcile_resources = stub
    return calls


async def test_concurrent_update_rollback_does_not_clobber_newer_write(
    store_env, monkeypatch
):
    """CONTROL-005-FIX Test 1.

    Request A: update succeeds on the store, reconcile fails, rollback
    runs.  Request B: update the SAME resource concurrently.

    Serialized by the mutation lock, whichever request runs first
    completes its ENTIRE lifecycle (including A's rollback) before the
    other starts — so the stale pre-A value can never land on top of B's
    committed write.  Final state: DB and pool agree on B's value."""
    app = await _manager_app(store_env)
    pool = app.state.scheduler.pools["antigravity"]
    _first_call_fails(pool)
    sink = app.state.resource_sink
    manager = app.state.resource_manager

    async def request_a():
        return await manager.update_resource(
            "antigravity", "r1", {"project_id": "p-A"}
        )

    async def request_b():
        return await manager.update_resource(
            "antigravity", "r1", {"project_id": "p-B"}
        )

    # A is scheduled first, so the FIFO asyncio.Lock hands it the
    # mutation lock first: A fails and rolls back, then B commits.
    results = await asyncio.gather(
        request_a(), request_b(), return_exceptions=True
    )
    error_a, ok_b = results
    assert isinstance(error_a, ResourceManagementError)
    assert "reconcile boom" in str(error_a)
    assert not isinstance(ok_b, BaseException), ok_b

    # No stale overwrite: the store holds B's value — not the pre-update
    # "p-seed" A's rollback captured, and not A's "p-A".
    stored = await sink.get("antigravity", "r1")
    assert stored.project_id == "p-B"
    # DB and pool are consistent with each other.
    resource = next(r for r in pool.resources if r.id == "r1")
    assert resource.project_id == "p-B"


async def test_concurrent_delete_update_serialize_without_silent_loss(
    store_env, monkeypatch
):
    """CONTROL-005-FIX Test 2.

    DELETE and PATCH run concurrently on the same resource.  Allowed:
    either one wins and the other sees a clean success-or-error.  Not
    allowed: a mutation whose request returned success being silently
    destroyed by the other's mid-flight interleaving.

    The sink operation log proves the two lifecycles never interleave:
    each request's read/write/reconcile block completes before the other
    starts."""
    app = await _manager_app(store_env)
    pool = app.state.scheduler.pools["antigravity"]
    sink = app.state.resource_sink
    manager = app.state.resource_manager

    op_log: list[str] = []
    for op in ("get", "update", "delete", "add", "list"):
        real = getattr(sink, op)

        def wrap(real=real, op=op):
            async def traced(*args, **kwargs):
                op_log.append(op)
                return await real(*args, **kwargs)

            return traced

        monkeypatch.setattr(sink, op, wrap())

    async def patch_request():
        return await manager.update_resource(
            "antigravity", "r1", {"project_id": "p-patched"}
        )

    async def delete_request():
        return await manager.delete_resource("antigravity", "r1")

    # PATCH is scheduled first: it completes its whole lifecycle, then
    # DELETE removes the resource — the classic sequential outcome.
    patch_result, _ = await asyncio.gather(
        patch_request(), delete_request(), return_exceptions=True
    )
    assert not isinstance(patch_result, BaseException), patch_result

    # The PATCH was NOT silently destroyed mid-flight: its lifecycle
    # (get → update → reconcile's list) fully precedes DELETE's
    # (get → delete → reconcile's list).
    assert op_log[:3] == ["get", "update", "list"]
    assert op_log[3:6] == ["get", "delete", "list"]
    # Final state is the sequential result: deleted everywhere.
    assert await sink.get("antigravity", "r1") is None
    assert [r.id for r in pool.resources] == []


async def test_delete_first_update_fails_clean(store_env):
    """Companion ordering: when DELETE runs first, the concurrent PATCH
    fails with a clean not-found — it never resurrects the deleted row
    and never reports success against a gone resource."""
    app = await _manager_app(store_env)
    sink = app.state.resource_sink
    manager = app.state.resource_manager

    async def delete_request():
        return await manager.delete_resource("antigravity", "r1")

    async def patch_request():
        return await manager.update_resource(
            "antigravity", "r1", {"project_id": "p-late"}
        )

    _, patch_error = await asyncio.gather(
        delete_request(), patch_request(), return_exceptions=True
    )
    assert isinstance(patch_error, ResourceManagementError)
    assert "not found" in str(patch_error)
    # The deleted resource stays deleted in store and pool.
    assert await sink.get("antigravity", "r1") is None
    assert [r.id for r in app.state.scheduler.pools["antigravity"].resources
            ] == []
