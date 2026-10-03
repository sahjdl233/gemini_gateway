"""Provider-aware resource control plane (CONTROL-001 / DB-RESOURCE-014).

Part D verification: two providers, the SAME resource_id, one repository —
repository rows coexist, runtime pools stay independent, Admin mutations
hit exactly the addressed (provider, resource_id).

The legacy antigravity-default routes keep working (deprecated compat).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

ADMIN = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-token")
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
            "fake": {"enabled": True, "resources": []},
            },
            "resource_bootstrap": {"enabled": True, "mode": "import"},
        },
        config_path=tmp_path / "config.yaml",
    )
    return TestClient(app)


def pools(app):
    return app.state.scheduler.pools


def test_same_resource_id_coexists_in_repository_and_runtime(client):
    """Repository identity (provider, resource_id): both r1 rows exist and
    neither runtime pool is overwritten by the other."""
    import asyncio

    sink = client.app.state.resource_sink

    stored = asyncio.run(sink.list())
    assert [(d.provider, d.id) for d in stored] == [
        ("antigravity", "r1"),
        ("gemini_cli", "r1"),
    ]
    ant_pool = pools(client.app)["antigravity"].resources
    gem_pool = pools(client.app)["gemini_cli"].resources
    assert [r.id for r in ant_pool] == ["r1"]
    assert [r.id for r in gem_pool] == ["r1"]
    assert ant_pool[0].project_id == "p-ant"
    assert gem_pool[0].tier == "paid"


def test_scoped_get_hits_exactly_one_identity(client):
    got_ant = client.get(
        "/admin/resources/antigravity/r1", headers=ADMIN
    )
    got_gem = client.get(
        "/admin/resources/gemini_cli/r1", headers=ADMIN
    )
    assert got_ant.status_code == 200 and got_gem.status_code == 200
    assert got_ant.json()["provider"] == "antigravity"
    assert got_ant.json()["project_id"] == "p-ant"
    assert got_gem.json()["provider"] == "gemini_cli"
    assert got_gem.json()["tier"] == "paid"


def test_scoped_patch_hits_only_addressed_identity(client):
    response = client.patch(
        "/admin/resources/gemini_cli/r1",
        json={"tier": "free"},
        headers=ADMIN,
    )
    assert response.status_code == 200, response.text
    assert response.json()["tier"] == "free"

    import asyncio

    sink = client.app.state.resource_sink
    gem = asyncio.run(sink.get("gemini_cli", "r1"))
    ant = asyncio.run(sink.get("antigravity", "r1"))
    assert gem.tier == "free"
    # The antigravity identity is untouched.
    assert ant.project_id == "p-ant"
    assert pools(client.app)["antigravity"].resources[0].project_id == "p-ant"


def test_scoped_delete_hits_only_addressed_identity(client):
    response = client.delete(
        "/admin/resources/antigravity/r1", headers=ADMIN
    )
    assert response.status_code == 204

    import asyncio

    sink = client.app.state.resource_sink
    assert asyncio.run(sink.get("antigravity", "r1")) is None
    assert asyncio.run(sink.get("gemini_cli", "r1")) is not None
    assert pools(client.app)["antigravity"].resources == []
    assert [r.id for r in pools(client.app)["gemini_cli"].resources] == ["r1"]


def test_scoped_create_same_id_under_second_provider(client):
    """POST is provider-explicit: the same id under a new provider is a
    new identity, not a duplicate."""
    response = client.post(
        "/admin/resources",
        json={"provider": "fake", "id": "r1", "scenario": "failure"},
        headers=ADMIN,
    )
    assert response.status_code == 201, response.text
    assert response.json()["provider"] == "fake"

    import asyncio

    stored = asyncio.run(client.app.state.resource_sink.list())
    assert [(d.provider, d.id) for d in stored] == [
        ("antigravity", "r1"),
        ("fake", "r1"),
        ("gemini_cli", "r1"),
    ]
    assert [r.id for r in pools(client.app)["fake"].resources] == ["r1"]


def test_unknown_provider_fails_closed(client):
    response = client.post(
        "/admin/resources",
        json={"provider": "does_not_exist", "id": "r1"},
        headers=ADMIN,
    )
    assert response.status_code == 400


def test_scoped_mutations_on_unknown_provider_fail(client):
    """Unknown providers fail closed: create → 400 (Part C style),
    scoped reads/mutations → 400 'unknown or unmanaged provider'."""
    assert (
        client.post(
            "/admin/resources",
            json={"provider": "does_not_exist", "id": "r1"},
            headers=ADMIN,
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/admin/resources/does_not_exist/r1", headers=ADMIN
        ).status_code
        == 400
    )
    assert (
        client.patch(
            "/admin/resources/does_not_exist/r1",
            json={},
            headers=ADMIN,
        ).status_code
        == 400
    )
    assert (
        client.delete(
            "/admin/resources/does_not_exist/r1", headers=ADMIN
        ).status_code
        == 400
    )


def test_scoped_mutations_on_known_provider_missing_resource_404(client):
    """A configured provider with no such resource is the 404 case."""
    assert (
        client.get(
            "/admin/resources/fake/nope", headers=ADMIN
        ).status_code
        == 404
    )
    assert (
        client.patch(
            "/admin/resources/fake/nope", json={}, headers=ADMIN
        ).status_code
        == 404
    )
    assert (
        client.delete(
            "/admin/resources/fake/nope", headers=ADMIN
        ).status_code
        == 404
    )


def test_list_resources_covers_all_providers(client):
    listing = client.get("/admin/resources", headers=ADMIN)
    assert listing.status_code == 200
    items = listing.json()
    assert {(i["provider"], i["id"]) for i in items} == {
        ("antigravity", "r1"),
        ("gemini_cli", "r1"),
    }


# -- deprecated single-segment compat routes resolve antigravity ----------------


def test_legacy_single_segment_routes_still_work(client):
    got = client.get("/admin/resources/r1", headers=ADMIN)
    assert got.status_code == 200
    assert got.json()["provider"] == "antigravity"

    patched = client.patch(
        "/admin/resources/r1", json={"project_id": "p-legacy"}, headers=ADMIN
    )
    assert patched.status_code == 200
    assert patched.json()["project_id"] == "p-legacy"

    # The gemini_cli identity was never touched by the legacy route.
    scoped = client.get(
        "/admin/resources/gemini_cli/r1", headers=ADMIN
    )
    assert scoped.json()["tier"] == "paid"
