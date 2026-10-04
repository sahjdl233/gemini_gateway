"""CONTROL-006-FIX-1: provider auth-adapter lifecycle invalidation.

A provider's auth-adapter cache is keyed by ``resource.id`` and holds the
OAuth lifecycle state (access token, rotated refresh token).  When a
resource definition is replaced (credential rebind A→B, disable, any
definition change) or removed, that state must not outlive the definition
it was created for — otherwise the first refresh after a rebind would
authenticate with the OLD credential's rotated token, and a
delete-plus-same-id-recreate would inherit the previous OAuth session.

The management layer compares pre/post reconcile definitions and calls the
optional ``provider.invalidate_resource(resource_id)`` capability after a
successful pool application; these tests drive that end to end against
the antigravity provider (the pattern is shared by firebase/gemini_cli).
"""

from __future__ import annotations

import asyncio

import pytest

from app.main import create_app
from core.credential import Credential, CredentialType


@pytest.fixture
def env(tmp_path, monkeypatch):
    """bootstrap-enabled memory deployment with an admin token."""
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


def _oauth_credential(credential_id: str, refresh_token: str) -> Credential:
    return Credential(
        id=credential_id,
        type=CredentialType.OAUTH,
        payload={
            "refresh_token": refresh_token,
            "client_id": "client-id",
            "client_secret": "client-secret",
        },
    )


def _pool_resource(app, resource_id):
    return next(
        resource
        for resource in app.state.scheduler.pools["antigravity"].resources
        if resource.id == resource_id
    )


async def _fresh_adapter(app, resource_id):
    """Adapter as a NEW request would see it after the mutation."""
    provider = app.state.scheduler.providers["antigravity"]
    return await provider._adapter_for(_pool_resource(app, resource_id))


def _capture_refresh(oauth, captured):
    """Stub the token-endpoint exchange, recording the refresh grant."""

    async def fake_exchange(resource, refresh_token, material):
        captured["refresh_token"] = refresh_token
        captured["material"] = dict(material)
        return "access-token", 3600, None

    oauth._exchange = fake_exchange


async def test_rebind_uses_new_credential_not_old_rotated_token(env):
    """Test 1: credential rebind A→B.

    r2 is bound to cred-a; its adapter accumulates OAuth runtime state
    including a rotated refresh token from A's session.  PATCHing the
    resource to cred-b must drop that adapter; the FIRST refresh on the
    fresh adapter must present cred-b's refresh_token to the token
    endpoint — never A's rotated token."""
    app = await asyncio.to_thread(make_app, env)
    store = app.state.credential_store
    store.add(_oauth_credential("cred-a", "rt-A"))
    store.add(_oauth_credential("cred-b", "rt-B"))

    manager = app.state.resource_manager
    created = await manager.create_resource(
        {"id": "r2", "project_id": "p2", "credential_id": "cred-a"},
        provider_id="antigravity",
    )
    provider = app.state.scheduler.providers["antigravity"]
    adapter = await provider._adapter_for(created)
    oauth = adapter.auth
    # OAuth runtime state from credential A's session (as a real refresh
    # rotation would leave it — AUTH-014 persisted it to cred-a first).
    oauth._access_token = "token-from-A"
    oauth._rotated_refresh_token = "rotated-from-A"

    await manager.update_resource(
        "antigravity", "r2", {"credential_id": "cred-b"}
    )

    # The old adapter is gone from the provider cache...
    assert "r2" not in provider._adapters
    new_adapter = await _fresh_adapter(app, "r2")
    assert new_adapter is not adapter
    # ...and carries none of A's OAuth state.
    assert new_adapter.auth._access_token is None
    assert new_adapter.auth._rotated_refresh_token is None

    # The first refresh presents B's material to the token endpoint.
    captured: dict = {}
    _capture_refresh(new_adapter.auth, captured)
    resource = _pool_resource(app, "r2")
    token = await new_adapter.auth.get_access_token(resource)
    assert token == "access-token"
    assert captured["refresh_token"] == "rt-B"
    assert captured["refresh_token"] != "rotated-from-A"
    assert captured["material"]["client_id"] == "client-id"


async def test_delete_recreate_same_id_starts_fresh(env):
    """Test 2: delete + recreate with the same id.

    The adapter OAuth state of the deleted resource must not leak into
    the recreated one: after re-creating r2 against credential B, the
    first refresh uses B's refresh token, not the deleted session's
    rotated token."""
    app = await asyncio.to_thread(make_app, env)
    store = app.state.credential_store
    store.add(_oauth_credential("cred-a", "rt-A"))
    store.add(_oauth_credential("cred-b", "rt-B"))

    manager = app.state.resource_manager
    created = await manager.create_resource(
        {"id": "r2", "project_id": "p2", "credential_id": "cred-a"},
        provider_id="antigravity",
    )
    provider = app.state.scheduler.providers["antigravity"]
    adapter = await provider._adapter_for(created)
    adapter.auth._access_token = "token-from-A"
    adapter.auth._rotated_refresh_token = "rotated-from-A"

    await manager.delete_resource("antigravity", "r2")
    assert "r2" not in provider._adapters

    recreated = await manager.create_resource(
        {"id": "r2", "project_id": "p2", "credential_id": "cred-b"},
        provider_id="antigravity",
    )
    new_adapter = await _fresh_adapter(app, "r2")
    assert new_adapter is not adapter
    assert new_adapter.auth._rotated_refresh_token is None

    captured: dict = {}
    _capture_refresh(new_adapter.auth, captured)
    token = await new_adapter.auth.get_access_token(recreated)
    assert token == "access-token"
    assert captured["refresh_token"] == "rt-B"


async def test_unchanged_resource_keeps_adapter_across_unrelated_write(env):
    """Invalidation is targeted: a PATCH to one resource must not drop
    the OAuth state of resources whose definitions did not change."""
    app = await asyncio.to_thread(make_app, env)
    store = app.state.credential_store
    store.add(_oauth_credential("cred-a", "rt-A"))

    manager = app.state.resource_manager
    created = await manager.create_resource(
        {"id": "r2", "project_id": "p2", "credential_id": "cred-a"},
        provider_id="antigravity",
    )
    provider = app.state.scheduler.providers["antigravity"]
    adapter = await provider._adapter_for(created)
    adapter.auth._access_token = "warm-token"

    # Touch the seed resource, not r2.
    await manager.update_resource(
        "antigravity", "r1", {"project_id": "p-seed-edited"}
    )

    assert provider._adapters.get("r2") is adapter
    assert adapter.auth._access_token == "warm-token"


async def test_disable_rebinds_and_reenable_all_invalidate(env):
    """Definition changes without a credential change (disable → enable)
    also replace the definition — the adapter OAuth state is dropped and
    the fresh adapter still resolves the same credential's material."""
    app = await asyncio.to_thread(make_app, env)
    store = app.state.credential_store
    store.add(_oauth_credential("cred-a", "rt-A"))

    manager = app.state.resource_manager
    created = await manager.create_resource(
        {"id": "r2", "project_id": "p2", "credential_id": "cred-a"},
        provider_id="antigravity",
    )
    provider = app.state.scheduler.providers["antigravity"]
    adapter = await provider._adapter_for(created)
    adapter.auth._rotated_refresh_token = "rotated-from-A"

    await manager.set_enabled("antigravity", "r2", False)
    assert "r2" not in provider._adapters
    disabled_adapter = await _fresh_adapter(app, "r2")
    assert disabled_adapter.auth._rotated_refresh_token is None

    await manager.set_enabled("antigravity", "r2", True)
    assert "r2" not in provider._adapters
    reenabled_adapter = await _fresh_adapter(app, "r2")
    assert reenabled_adapter is not disabled_adapter
    assert reenabled_adapter.auth._rotated_refresh_token is None
