"""Credential domain model tests (TASK-AUTH-002)."""

from __future__ import annotations

import pytest

from core.credential import (
    Credential,
    CredentialStore,
    CredentialType,
    DuplicateCredentialError,
    UnknownCredentialError,
    redact_payload,
)
from core.resource import Resource
from providers.fake import FakeResource


# -- Credential creation ----------------------------------------------------

def test_credential_types_exist():
    assert CredentialType.NONE.value == "none"
    assert CredentialType.API_KEY.value == "api_key"
    assert CredentialType.OAUTH.value == "oauth"


def test_credential_type_none_defaults():
    cred = Credential(id="empty-01")
    assert cred.type is CredentialType.NONE
    assert cred.payload == {}
    assert cred.created_at is not None
    assert cred.updated_at is not None


def test_credential_oauth_payload_round_trip():
    payload = {
        "refresh_token": "rt-secret",
        "client_id": "client-id-1",
        "client_secret": "client-secret-1",
    }
    cred = Credential(id="google-oauth-01", type=CredentialType.OAUTH, payload=payload)
    assert cred.id == "google-oauth-01"
    assert cred.type is CredentialType.OAUTH
    # payload keeps the real material for auth use
    assert cred.payload["refresh_token"] == "rt-secret"
    assert cred.payload["client_id"] == "client-id-1"


def test_credential_api_key_payload_round_trip():
    cred = Credential(
        id="firebase-01",
        type=CredentialType.API_KEY,
        payload={"api_key": "AIzaSyTEST", "app_id": "app-1", "debug_token": "dbg-1"},
    )
    assert cred.payload["api_key"] == "AIzaSyTEST"


def test_credential_accepts_type_from_string():
    cred = Credential.model_validate({"id": "c1", "type": "oauth", "payload": {}})
    assert cred.type is CredentialType.OAUTH


# -- redaction: secrets never leak through repr/str/redacted_dict ------------

SECRET_VALUES = [
    "rt-secret-value",
    "client-secret-value",
    "AIzaSyTESTKEY",
    "dbg-token-value",
    "short-access-token",
]

SECRET_KEYS = [
    "refresh_token",
    "client_secret",
    "api_key",
    "debug_token",
    "access_token",
]


def test_credential_payload_secrets():
    payload = dict(zip(SECRET_KEYS, SECRET_VALUES))
    payload["client_id"] = "public-client-id"
    payload["project_id"] = "public-project"
    cred = Credential(id="c1", type=CredentialType.OAUTH, payload=payload)

    for render in (repr(cred), str(cred), str(cred.redacted_dict())):
        for secret in SECRET_VALUES:
            assert secret not in render
        # non-secret identifiers stay visible
        assert "public-client-id" in render
        assert "public-project" in render


def test_redact_payload_masks_secret_shaped_keys():
    redacted = redact_payload({
        "session_token": "s",
        "some_password": "p",
        "weird_cookie": "c",
        "pinned_model": "gemini",
    })
    assert redacted["session_token"] == "***"
    assert redacted["some_password"] == "***"
    assert redacted["weird_cookie"] == "***"
    assert redacted["pinned_model"] == "gemini"


def test_redact_payload_does_not_mutate_original():
    payload = {"api_key": "AIzaSyTEST"}
    redact_payload(payload)
    assert payload["api_key"] == "AIzaSyTEST"


# -- Resource.credential_id ---------------------------------------------------

def test_resource_credential_id_defaults_to_none():
    res = Resource(id="r1", provider="fake")
    assert res.credential_id is None


def test_resource_can_reference_credential():
    res = Resource(id="r1", provider="fake", credential_id="google-oauth-01")
    assert res.credential_id == "google-oauth-01"


def test_fake_resource_keeps_no_credential():
    res = FakeResource(id="fake-01", provider="fake", scenario="success")
    assert res.credential_id is None


# -- Cardinality: Resource → 0..1, Credential → N (model level) --------------

def test_many_resources_may_reference_one_credential():
    """Credential → N Resources is expressible without any join structure."""
    shared = Credential(id="shared-01", type=CredentialType.OAUTH, payload={})
    a = Resource(id="a", provider="gemini_cli", credential_id=shared.id)
    b = Resource(id="b", provider="gemini_cli", credential_id=shared.id)
    c = Resource(id="c", provider="antigravity", credential_id=shared.id)
    assert a.credential_id == b.credential_id == c.credential_id == "shared-01"


def test_resource_holds_at_most_one_credential_reference():
    res = Resource(id="r1", provider="gemini_cli", credential_id="cred-1")
    assert isinstance(res.credential_id, str)  # a single scalar reference


# -- CredentialStore ----------------------------------------------------------

def test_store_add_get():
    store = CredentialStore()
    cred = Credential(id="c1", type=CredentialType.OAUTH)
    store.add(cred)
    assert store.get("c1") is cred
    assert "c1" in store
    assert len(store) == 1


def test_store_duplicate_id_rejected():
    store = CredentialStore()
    store.add(Credential(id="c1"))
    with pytest.raises(DuplicateCredentialError):
        store.add(Credential(id="c1"))


def test_store_get_unknown_returns_none():
    store = CredentialStore()
    assert store.get("missing") is None
    with pytest.raises(UnknownCredentialError):
        store.require("missing")


def test_store_update_payload_bumps_updated_at():
    store = CredentialStore()
    cred = store.add(Credential(id="c1", type=CredentialType.OAUTH, payload={"a": 1}))
    before = cred.updated_at
    store.update_payload("c1", {"a": 2})
    assert cred.payload == {"a": 2}
    assert cred.updated_at >= before


def test_store_remove_idempotent():
    store = CredentialStore()
    store.add(Credential(id="c1"))
    store.remove("c1")
    store.remove("c1")
    assert len(store) == 0


def test_store_list():
    store = CredentialStore()
    store.add(Credential(id="c1"))
    store.add(Credential(id="c2", type=CredentialType.API_KEY))
    assert [c.id for c in store.list()] == ["c1", "c2"]
