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
