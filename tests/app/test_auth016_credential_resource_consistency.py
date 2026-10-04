"""TASK-AUTH-016: credential payload mutation & resource binding consistency.

Facet A — PATCH credential → invalidate affected adapters: a resource's
auth adapter holds OAuth runtime state whose rotated refresh token would
otherwise keep winning over the store payload; an operator's payload
replacement must take effect on the NEXT request.

Facet B — credential/resource mutation consistency:

* a resource write with a set ``credential_id`` validates credential
  existence at the write boundary (the ``enabled + missing credential``
  half-broken state is no longer creatable via the Admin API);
* credential DELETE/PATCH run under the manager's ``mutation_lock``,
  serializing them against resource mutations — the reference check and
  the removal cannot be interleaved by a concurrent bind.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from core.credential import Credential, CredentialType


ADMIN = {"Authorization": "Bearer test-token"}

OAUTH_PAYLOAD = {
    "refresh_token": "rt-a",
    "client_id": "client-id",
    "client_secret": "client-secret",
}


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


def add_credential(store, credential_id, refresh_token="rt-a"):
    store.add(
        Credential(
            id=credential_id,
            type=CredentialType.OAUTH,
            payload={**OAUTH_PAYLOAD, "refresh_token": refresh_token},
        )
    )


# -- Facet A: PATCH credential invalidates bound adapters --------------------------


def test_patch_credential_invalidates_bound_adapters(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-token")
    app = make_app(tmp_path / "config.yaml")
    store = app.state.credential_store
    add_credential(store, "cred-a")

    client = TestClient(app)

    async def setup():
        manager = app.state.resource_manager
        created = await manager.create_resource(
            {"id": "r2", "project_id": "p2", "credential_id": "cred-a"},
            provider_id="antigravity",
        )
        return created

    created = asyncio.run(setup())
    provider = app.state.scheduler.providers["antigravity"]
    adapter = asyncio.run(provider._adapter_for(created))
    # OAuth runtime state from the pre-PATCH session.
    adapter.auth._access_token = "token-old"
    adapter.auth._rotated_refresh_token = "rotated-old"

    response = client.patch(
        "/admin/credentials/cred-a",
        json={"payload": {**OAUTH_PAYLOAD, "refresh_token": "rt-new"}},
        headers=ADMIN,
    )
    assert response.status_code == 200, response.text

    # The bound resource's adapter is gone from the provider cache...
    assert "r2" not in provider._adapters

    async def first_refresh():
        new_adapter = await provider._adapter_for(created)
        assert new_adapter is not adapter
        assert new_adapter.auth._rotated_refresh_token is None
        # First refresh after the payload replacement presents the NEW
        # refresh token — the old rotated token cannot win anymore.

        async def fake_exchange(resource, refresh_token, material):
            return "access-token", 3600, None

        new_adapter.auth._exchange = fake_exchange
        return await new_adapter.auth.get_access_token(created)

    assert asyncio.run(first_refresh()) == "access-token"
    assert created.credential_id == "cred-a"


def test_patch_unbound_credential_is_a_noop_for_adapters(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "test-token")
    app = make_app(tmp_path / "config.yaml")
    store = app.state.credential_store
    add_credential(store, "cred-unbound")
    client = TestClient(app)

    response = client.patch(
        "/admin/credentials/cred-unbound",
        json={"payload": {**OAUTH_PAYLOAD, "refresh_token": "rt-new"}},
        headers=ADMIN,
    )
    assert response.status_code == 200
    # No resource references it: no adapters to invalidate, and the
    # pools are untouched.
    assert all(
        not pool.resources for pool in app.state.scheduler.pools.values()
    ) or all(
        resource.credential_id != "cred-unbound"
        for pool in app.state.scheduler.pools.values()
        for resource in pool.resources
    )


# -- Facet B1: resource writes validate credential existence -----------------------


def test_resource_write_rejects_missing_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-token")
    app = make_app(tmp_path / "config.yaml")
    client = TestClient(app)
    pool = app.state.scheduler.pools["antigravity"]

    # PATCH: binding a nonexistent credential is rejected.
    patched = client.patch(
        "/admin/resources/antigravity/r1",
        json={"credential_id": "cred-missing"},
        headers=ADMIN,
    )
    assert patched.status_code == 400, patched.text
    assert "cred-missing" in patched.json()["detail"]
    assert "not found" in patched.json()["detail"]
    # No residue: the pool resource is unchanged.
    assert pool.resources[0].credential_id is None

    # CREATE: same boundary.
    created = client.post(
        "/admin/resources",
        json={"id": "r2", "project_id": "p2", "credential_id": "cred-missing"},
        headers=ADMIN,
    )
    assert created.status_code == 400
    stored = asyncio.run(app.state.resource_sink.get("antigravity", "r2"))
    assert stored is None

    # Binding an EXISTING credential works.
    add_credential(app.state.credential_store, "cred-a")
    ok = client.patch(
        "/admin/resources/antigravity/r1",
        json={"credential_id": "cred-a"},
        headers=ADMIN,
    )
    assert ok.status_code == 200, ok.text
    assert pool.resources[0].credential_id == "cred-a"


def test_legacy_write_rejects_missing_credential(tmp_path, monkeypatch):
    """The legacy (bootstrap-disabled) in-place path validates too."""
    monkeypatch.setenv("ADMIN_TOKEN", "test-token")
    app = create_app(
        config={
            "providers": {
                "antigravity": {
                    "enabled": True,
                    "resources": [{"id": "r1", "project_id": "p"}],
                }
            }
        },
        config_path=tmp_path / "config.yaml",
    )
    client = TestClient(app)
    patched = client.patch(
        "/admin/resources/r1",
        json={"credential_id": "cred-missing"},
        headers=ADMIN,
    )
    assert patched.status_code == 400
    assert "not found" in patched.json()["detail"]
    # The in-place mutation left no residue.
    assert app.state.scheduler.pools["antigravity"].resources[
        0
    ].credential_id is None


# -- Facet B2: credential mutations serialize against resource writes --------------


async def _bind(client, app):
    return await client.patch(
        "/admin/resources/antigravity/r1",
        json={"credential_id": "cred-a"},
        headers=ADMIN,
    )


async def _delete_credential(client):
    return await client.delete("/admin/credentials/cred-a", headers=ADMIN)


async def test_bind_wins_over_delete_leaves_no_dangling(
    tmp_path, monkeypatch
):
    """Bind scheduled before delete — with the race window FORCED open.

    Choreography: the bind parks inside its reconcile (validation has
    already passed, the pool has NOT been updated yet); the delete then
    runs and must find the resource still unreferenced in the pool.

    Without the mutation lock the delete removes the credential inside
    that window: the bind's response was success yet the resource ends
    up bound to a deleted credential (dangling), and the flag records
    the interleave.  With the lock the delete cannot start inside the
    bind's lifecycle (the gate times out), the bind commits a live
    reference, and the delete then fails 409 — no dangling state."""
    monkeypatch.setenv("ADMIN_TOKEN", "test-token")
    app = await asyncio.to_thread(make_app, tmp_path / "config.yaml")
    add_credential(app.state.credential_store, "cred-a")
    pool = app.state.scheduler.pools["antigravity"]
    flags = {"interleaved": False}
    bind_parked = asyncio.Event()
    delete_done = asyncio.Event()

    real_reconcile = pool.reconcile_resources

    async def gated_reconcile(new_resources):
        # Park the bind between its (passed) credential validation and
        # the pool update — the reference is not yet visible.
        bind_parked.set()
        try:
            await asyncio.wait_for(delete_done.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass  # serialized: the delete never ran inside the window
        else:
            flags["interleaved"] = True
        return await real_reconcile(new_resources)

    monkeypatch.setattr(pool, "reconcile_resources", gated_reconcile)

    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://t")

    async def bind_request():
        try:
            return await _bind(client, app)
        finally:
            delete_done.set()

    bind_response, delete_response = await asyncio.gather(
        bind_request(), _delete_credential(client)
    )
    await client.aclose()

    # The lock must keep the delete outside the bind's lifecycle.
    assert not flags["interleaved"]
    assert bind_response.status_code == 200, bind_response.text
    assert delete_response.status_code == 409
    # Invariant: every bound credential exists.
    assert app.state.credential_store.get("cred-a") is not None
    assert pool.resources[0].credential_id == "cred-a"


async def test_delete_wins_and_bind_fails_clean(tmp_path, monkeypatch):
    """Delete scheduled before bind: the unreferenced credential is
    removed, then the bind's existence validation fails 400 — the write
    never lands on a deleted credential."""
    monkeypatch.setenv("ADMIN_TOKEN", "test-token")
    app = await asyncio.to_thread(make_app, tmp_path / "config.yaml")
    add_credential(app.state.credential_store, "cred-a")
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://t")

    delete_response, bind_response = await asyncio.gather(
        _delete_credential(client), _bind(client, app)
    )
    await client.aclose()

    assert delete_response.status_code == 204
    assert bind_response.status_code == 400
    assert "not found" in bind_response.json()["detail"]
    # Invariant: the credential stayed deleted, the resource unbound.
    assert app.state.credential_store.get("cred-a") is None
    assert (
        app.state.scheduler.pools["antigravity"].resources[0].credential_id
        is None
    )
    # No half-written store row either.
    stored = await app.state.resource_sink.get("antigravity", "r1")
    assert stored.credential_id is None
