"""Runtime management API tests (no upstream network).

WEBUI-001: the Admin Resource surface manages *runtime* configuration only.
Long-lived credential material belongs to Credential and is reached through
``credential_id``; the Resource API must refuse secret fields outright and the
WebUI must not offer them as inputs.

WEBUI-002: ``/admin/`` is served from the compiled Vue 3 + Vite bundle in
``webui/dist`` (no Python inline HTML), with assets under ``/admin/assets/``.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from core.credential_encryption import CredentialEncryptor
from core.credential_postgres import PostgreSQLCredentialRepository
from tests.core._fake_postgres import FakePostgres

ADMIN = {"Authorization": "Bearer test-admin-secret"}


def make_config(tmp_path, monkeypatch, *, durable: bool = False):
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-secret")
    config = {
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
    if durable:
        config["credential_repository"] = {"backend": "postgres"}
    return config


def _client(tmp_path, monkeypatch, *, durable: bool = False):
    # Existing factory registration makes this path deterministic without a
    # real Antigravity credential or network request.
    return TestClient(
        create_app(
            make_config(tmp_path, monkeypatch, durable=durable),
            config_path=tmp_path / "config.yaml",
        )
    )


def test_admin_requires_authentication(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/admin/resources")
    assert response.status_code == 401


# -- WebUI: built Vue SPA is served, not Python inline HTML (WEBUI-002 §5/§8) ---
SECRET_FIELD_NAMES = (
    "access_token",
    "refresh_token",
    "client_id",
    "client_secret",
    "api_key",
    "debug_token",
)


def _webui_dist() -> Path:
    return Path(__file__).resolve().parents[2] / "webui" / "dist"


def test_admin_page_serves_built_spa_entry(tmp_path, monkeypatch):
    """GET /admin/ returns webui/dist/index.html, not inline Python HTML."""
    index = _webui_dist() / "index.html"
    if not index.is_file():
        pytest.skip("webui/dist not built; run `npm install && npm run build` in webui/")
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/admin/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == index.read_text(encoding="utf-8")
    # The Vite entry mounts an empty root div; the old inline page shipped
    # server-rendered tables and an inline <script> instead.
    assert '<div id="app"></div>' in response.text
    assert "<table" not in response.text
    assert "addEventListener" not in response.text


def test_admin_page_assets_are_served_with_correct_content_types(
    tmp_path, monkeypatch
):
    """Hashed build assets resolve under /admin/assets/ (WEBUI-002 §5)."""
    index = _webui_dist() / "index.html"
    if not index.is_file():
        pytest.skip("webui/dist not built; run `npm install && npm run build` in webui/")
    body = index.read_text(encoding="utf-8")
    urls = re.findall(r'(?:src|href)="(/admin/assets/[^"]+)"', body)
    assert urls, "built index.html must reference /admin/assets/..."
    with _client(tmp_path, monkeypatch) as client:
        for url in urls:
            asset = client.get(url)
            assert asset.status_code == 200, url
            assert asset.content, url
        script = client.get(next(u for u in urls if u.endswith(".js")))
    assert "javascript" in script.headers["content-type"]


def test_admin_spa_fallback_serves_entry_for_client_routes(tmp_path, monkeypatch):
    """SPA fallback never shadows the Admin API (WEBUI-002 §5)."""
    index = _webui_dist() / "index.html"
    if not index.is_file():
        pytest.skip("webui/dist not built; run `npm install && npm run build` in webui/")
    entry = index.read_text(encoding="utf-8")
    with _client(tmp_path, monkeypatch) as client:
        # An unknown client-side route falls back to the SPA entry...
        assert client.get("/admin/some-client-route").text == entry
        # ...while real API paths keep their own semantics.
        assert client.get("/admin/resources").status_code == 401
        assert client.get(
            "/admin/resources", headers=ADMIN
        ).status_code == 200
        missing = client.get("/admin/resources/does-not-exist", headers=ADMIN)
    assert missing.status_code == 404


def test_built_admin_bundle_has_no_resource_credential_inputs(tmp_path, monkeypatch):
    """The shipped bundle offers no Resource credential form fields (§8)."""
    assets = _webui_dist() / "assets"
    if not assets.is_dir():
        pytest.skip("webui/dist not built; run `npm install && npm run build` in webui/")
    sources = _webui_dist() / "index.html"
    bundles = [p for p in assets.iterdir() if p.suffix in (".js", ".css")]
    assert bundles, "expected hashed JS/CSS bundles in webui/dist/assets"
    combined = sources.read_text(encoding="utf-8") + "".join(
        p.read_text(encoding="utf-8") for p in bundles
    )
    for field in SECRET_FIELD_NAMES:
        assert f'name="{field}"' not in combined
    # Placeholder text on the Credential payload editor is the one legitimate
    # mention of a secret-shaped key, and it lives in the credential form.
    for secret in ("access-secret-a", "refresh-secret-a", "client-secret-a"):
        assert secret not in combined


# -- Resource API: runtime fields only (WEBUI-001 §1) ----------------------------
def test_resource_list_exposes_runtime_fields_and_no_secrets(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        listing = client.get("/admin/resources", headers=ADMIN)
    assert listing.status_code == 200
    body = listing.text
    for secret in (
        "access-secret-a",
        "refresh-secret-a",
        "client-secret-a",
        "client-id-a",
    ):
        assert secret not in body
    item = listing.json()[0]
    for key in (
        "id",
        "provider",
        "enabled",
        "credential_id",
        "health",
        "cooldown_until",
        "in_flight",
        "total_requests",
        "total_failures",
    ):
        assert key in item
    for key in ("access_token", "refresh_token", "client_id", "client_secret"):
        assert key not in item


def test_admin_can_crud_and_toggle_resources(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/admin/resources",
            headers=ADMIN,
            json={"id": "account-b", "project_id": "project-b"},
        )
        assert created.status_code == 201
        assert created.json()["project_id"] == "project-b"
        patched = client.patch(
            "/admin/resources/account-b",
            headers=ADMIN,
            json={"project_id": "project-b-updated", "enabled": False},
        )
        assert patched.status_code == 200
        assert patched.json()["project_id"] == "project-b-updated"
        assert patched.json()["enabled"] is False
        assert (
            client.post("/admin/resources/account-b/disable", headers=ADMIN).json()[
                "enabled"
            ]
            is False
        )
        assert (
            client.post("/admin/resources/account-b/enable", headers=ADMIN).json()[
                "enabled"
            ]
            is True
        )
        deleted = client.delete("/admin/resources/account-b", headers=ADMIN)
        assert deleted.status_code == 204
        assert "account-b" not in client.get("/admin/resources", headers=ADMIN).text


# -- Resource API: secret injection is rejected (WEBUI-001 §1/§3) ---------------
SECRET_FIELDS = (
    "access_token",
    "refresh_token",
    "client_id",
    "client_secret",
    "api_key",
    "debug_token",
)


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_post_resource_with_secret_field_returns_400(tmp_path, monkeypatch, field):
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/admin/resources",
            headers=ADMIN,
            json={"id": "injected", field: "super-secret-value"},
        )
        assert response.status_code == 400
        assert "super-secret-value" not in response.text
        assert field in response.text
        listing = client.get("/admin/resources", headers=ADMIN)
        assert "injected" not in listing.text


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_patch_resource_with_secret_field_returns_400(tmp_path, monkeypatch, field):
    with _client(tmp_path, monkeypatch) as client:
        response = client.patch(
            "/admin/resources/account-a",
            headers=ADMIN,
            json={field: "super-secret-value"},
        )
    assert response.status_code == 400
    assert "super-secret-value" not in response.text
    assert field in response.text


def test_rejected_resource_mutation_does_not_write_secrets_to_yaml(
    tmp_path, monkeypatch
):
    path = tmp_path / "config.yaml"
    with _client(tmp_path, monkeypatch) as client:
        rejected = client.patch(
            "/admin/resources/account-a",
            headers=ADMIN,
            json={"refresh_token": "must-not-persist"},
        )
        assert rejected.status_code == 400
    assert (
        not path.exists()
        or "must-not-persist" not in path.read_text(encoding="utf-8")
    )


def test_management_errors_do_not_leak_secrets(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.patch(
            "/admin/resources/missing",
            headers=ADMIN,
            json={"project_id": "p"},
        )
        assert response.status_code == 404
        unsupported = client.patch(
            "/admin/resources/account-a",
            headers=ADMIN,
            json={"access_token": "must-not-leak"},
        )
        assert unsupported.status_code == 400
        assert "must-not-leak" not in unsupported.text


# -- PostgreSQL mode (WEBUI-001 §3) ----------------------------------------------
def _durable_client(tmp_path, monkeypatch):
    harness = FakePostgres()
    repo = PostgreSQLCredentialRepository(
        harness.connection_factory(),
        encryptor=CredentialEncryptor(os.urandom(32)),
    )
    monkeypatch.setattr("app.main.build_credential_store", lambda config: repo)
    client = TestClient(
        create_app(
            make_config(tmp_path, monkeypatch, durable=True),
            config_path=tmp_path / "config.yaml",
        )
    )
    return client, harness


def test_postgres_resource_mutation_rejects_secrets_and_keeps_yaml_clean(
    tmp_path, monkeypatch
):
    path = tmp_path / "config.yaml"
    client, _ = _durable_client(tmp_path, monkeypatch)
    with client:
        rejected = client.post(
            "/admin/resources",
            headers=ADMIN,
            json={
                "id": "durable-1",
                "credential_id": "durable-cred",
                "client_secret": "should-never-persist",
            },
        )
        assert rejected.status_code == 400
        assert "should-never-persist" not in rejected.text
        ok = client.post(
            "/admin/resources",
            headers=ADMIN,
            json={"id": "durable-2", "credential_id": "durable-cred"},
        )
        assert ok.status_code == 201
        assert "should-never-persist" not in ok.text
    text = path.read_text(encoding="utf-8")
    assert "should-never-persist" not in text


def test_postgres_mode_preserves_legacy_config_fields_but_adds_no_new_ones(
    tmp_path, monkeypatch
):
    """WEBUI-001 §4: existing legacy keys are not force-cleared."""
    path = tmp_path / "config.yaml"
    client, _ = _durable_client(tmp_path, monkeypatch)
    with client:
        assert (
            client.patch(
                "/admin/resources/account-a",
                headers=ADMIN,
                json={"project_id": "project-a-updated"},
            ).status_code
            == 200
        )
    text = path.read_text(encoding="utf-8")
    assert "project-a-updated" in text
    assert "access-secret-a" in text
    assert "refresh-secret-a" in text


# -- Resource persistence round trip (runtime fields only) ----------------------
def test_resource_persistence_round_trip(tmp_path, monkeypatch):
    config = make_config(tmp_path, monkeypatch)
    path = tmp_path / "config.yaml"
    with TestClient(create_app(config, config_path=path)) as client:
        response = client.post(
            "/admin/resources",
            headers=ADMIN,
            json={"id": "persistent", "credential_id": "cred-1", "project_id": "p"},
        )
        assert response.status_code == 201
    assert path.exists()
    persisted = path.read_text(encoding="utf-8")
    assert "persistent" in persisted
    assert "cred-1" in persisted
    with TestClient(create_app(None, config_path=path)) as restarted:
        response = restarted.get("/admin/resources", headers=ADMIN)
        assert any(item["id"] == "persistent" for item in response.json())


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
        disabled = client.post("/admin/resources/account-a/disable", headers=ADMIN)
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        selected = await pool.acquire()
        assert selected is None
        assert await provider.list_models() == []
        discovery.assert_not_awaited()
        enabled = client.post("/admin/resources/account-a/enable", headers=ADMIN)
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
        assert await provider.list_models() == []
        discovery.assert_awaited_once_with(resource)
        selected = await pool.acquire()
        assert selected is resource
        if selected is not None:
            await pool.release(selected)
