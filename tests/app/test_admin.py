"""Runtime management API tests (no upstream network)."""
from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.main import create_app


def make_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-secret")
    return {
        "providers": {
            "antigravity": {
                "enabled": True,
                "resources": [
                    {
                        "id": "account-a",
                        "access_token": "access-secret-a",
                        "refresh_token": "refresh-secret-a",
                        "client_id": "client-id-a",
                        "client_secret": "client-secret-a",
                        "project_id": "project-a",
                        "enabled": True,
                    }
                ],
            }
        }
    }


def _client(tmp_path, monkeypatch):
    # Existing factory registration makes this path deterministic without a
    # real Antigravity credential or network request.
    return TestClient(create_app(make_config(tmp_path, monkeypatch), config_path=tmp_path / "config.yaml"))


def test_admin_requires_authentication(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/admin/resources")
    assert response.status_code == 401


def test_admin_page_uses_password_inputs_for_credentials(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/admin/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    for field in ("access_token", "refresh_token", "client_secret"):
        assert f'name="{field}" type="password"' in response.text


def test_admin_can_crud_enable_disable_and_mask_credentials(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        headers = {"Authorization": "Bearer test-admin-secret"}
        listing = client.get("/admin/resources", headers=headers)
        assert listing.status_code == 200
        body = listing.text
        assert "access-secret-a" not in body
        assert "refresh-secret-a" not in body
        assert "client-secret-a" not in body
        assert listing.json()[0]["access_token"] == "configured"

        created = client.post(
            "/admin/resources",
            headers=headers,
            json={
                "id": "account-b",
                "access_token": "access-secret-b",
                "refresh_token": "refresh-secret-b",
                "client_secret": "client-secret-b",
                "project_id": "project-b",
            },
        )
        assert created.status_code == 201
        assert "access-secret-b" not in created.text

        patched = client.patch(
            "/admin/resources/account-b",
            headers=headers,
            json={"project_id": "project-b-updated", "enabled": False},
        )
        assert patched.status_code == 200
        assert patched.json()["project_id"] == "project-b-updated"
        assert patched.json()["enabled"] is False

        assert client.post("/admin/resources/account-b/disable", headers=headers).json()["enabled"] is False
        assert client.post("/admin/resources/account-b/enable", headers=headers).json()["enabled"] is True
        deleted = client.delete("/admin/resources/account-b", headers=headers)
        assert deleted.status_code == 204
        assert "account-b" not in client.get("/admin/resources", headers=headers).text


async def test_disabled_resource_is_not_selected_or_discovered_until_enabled(
    tmp_path, monkeypatch
):
    with _client(tmp_path, monkeypatch) as client:
        scheduler = client.app.state.scheduler
        manager = client.app.state.resource_manager
        pool = scheduler.pools["antigravity"]
        provider = scheduler.providers["antigravity"]
        resource = manager.get_resource("account-a")
        discovery = AsyncMock(return_value=[])
        provider.discovery.fetch_models = discovery

        disabled = client.post(
            "/admin/resources/account-a/disable",
            headers={"Authorization": "Bearer test-admin-secret"},
        )
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        selected = await pool.acquire()
        assert selected is None
        assert await provider.list_models() == []
        discovery.assert_not_awaited()

        enabled = client.post(
            "/admin/resources/account-a/enable",
            headers={"Authorization": "Bearer test-admin-secret"},
        )
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
        assert await provider.list_models() == []
        discovery.assert_awaited_once_with(resource)

        selected = await pool.acquire()
        assert selected is resource
        if selected is not None:
            await pool.release(selected)


def test_resource_persistence_round_trip(tmp_path, monkeypatch):
    config = make_config(tmp_path, monkeypatch)
    path = tmp_path / "config.yaml"
    with TestClient(create_app(config, config_path=path)) as client:
        response = client.post(
            "/admin/resources",
            headers={"Authorization": "Bearer test-admin-secret"},
            json={"id": "persistent", "access_token": "persistent-secret", "project_id": "p"},
        )
        assert response.status_code == 201
    assert path.exists()
    assert "persistent-secret" in path.read_text(encoding="utf-8")
    with TestClient(create_app(None, config_path=path)) as restarted:
        response = restarted.get(
            "/admin/resources",
            headers={"Authorization": "Bearer test-admin-secret"},
        )
        assert any(item["id"] == "persistent" for item in response.json())


def test_management_errors_do_not_leak_secrets(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.patch(
            "/admin/resources/missing",
            headers={"Authorization": "Bearer test-admin-secret"},
            json={"access_token": "must-not-leak"},
        )
        assert response.status_code == 404
        assert "must-not-leak" not in response.text
