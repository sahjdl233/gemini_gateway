"""Keyring / key rotation tests for the encryption boundary (AUTH-012).

Legacy single-key compatibility, multi-kid decryption, active-kid writes,
and fail-closed misconfiguration — all with dynamically generated keys.
"""

from __future__ import annotations

import base64
import os

import pytest

from core.credential_encryption import (
    CredentialDecryptionError,
    CredentialEncryptionConfigError,
    CredentialEncryptor,
    ENCRYPTION_KEY_ENV_VAR,
    ENCRYPTION_KEY_ID_ENV_VAR,
    ENCRYPTION_KEYS_ENV_VAR,
    load_encryption_keys,
)

PAYLOAD = {"refresh_token": "rt-secret", "client_id": "cid"}


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def key_env(monkeypatch, keys: dict, active: str | None = None):
    monkeypatch.setenv(ENCRYPTION_KEYS_ENV_VAR, json.dumps(keys))
    if active is not None:
        monkeypatch.setenv(ENCRYPTION_KEY_ID_ENV_VAR, active)


import json  # noqa: E402  (used by key_env)


# -- legacy single-key compatibility ----------------------------------------------


def test_legacy_env_still_works(monkeypatch):
    monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, b64(os.urandom(32)))
    monkeypatch.delenv(ENCRYPTION_KEYS_ENV_VAR, raising=False)
    monkeypatch.delenv(ENCRYPTION_KEY_ID_ENV_VAR, raising=False)

    encryptor = CredentialEncryptor.from_environment()

    assert encryptor.active_kid == "default"
    envelope = encryptor.encrypt_payload(PAYLOAD)
    assert envelope["kid"] == "default"
    assert encryptor.decrypt_payload(envelope) == PAYLOAD


def test_legacy_constructor_compatibility():
    encryptor = CredentialEncryptor(os.urandom(32))
    envelope = encryptor.encrypt_payload(PAYLOAD)
    assert envelope["kid"] == "default"
    assert encryptor.decrypt_payload(envelope) == PAYLOAD


# -- keyring loading ----------------------------------------------------------------


def test_keyring_env_loads_keys_and_active_kid(monkeypatch):
    keys = {"v1": os.urandom(32), "v2": os.urandom(32)}
    key_env(monkeypatch, {"v1": b64(keys["v1"]), "v2": b64(keys["v2"])}, "v2")

    loaded, active = load_encryption_keys()

    assert loaded == keys
    assert active == "v2"


def test_keyring_without_active_kid_fails(monkeypatch):
    key_env(monkeypatch, {"v1": b64(os.urandom(32))})
    with pytest.raises(CredentialEncryptionConfigError):
        load_encryption_keys()


def test_active_kid_not_in_keyring_fails(monkeypatch):
    key_env(monkeypatch, {"v1": b64(os.urandom(32))}, "v2")
    with pytest.raises(CredentialEncryptionConfigError):
        load_encryption_keys()


def test_invalid_keyring_json_fails(monkeypatch):
    monkeypatch.setenv(ENCRYPTION_KEYS_ENV_VAR, "not-json")
    monkeypatch.setenv(ENCRYPTION_KEY_ID_ENV_VAR, "v1")
    with pytest.raises(CredentialEncryptionConfigError):
        load_encryption_keys()


def test_keyring_wrong_key_length_fails(monkeypatch):
    key_env(monkeypatch, {"v1": b64(os.urandom(16))}, "v1")
    with pytest.raises(CredentialEncryptionConfigError):
        load_encryption_keys()


def test_keyring_empty_object_fails(monkeypatch):
    key_env(monkeypatch, {}, "v1")
    with pytest.raises(CredentialEncryptionConfigError):
        load_encryption_keys()


def test_key_id_without_keyring_fails(monkeypatch):
    monkeypatch.delenv(ENCRYPTION_KEYS_ENV_VAR, raising=False)
    monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, b64(os.urandom(32)))
    monkeypatch.setenv(ENCRYPTION_KEY_ID_ENV_VAR, "default")
    with pytest.raises(CredentialEncryptionConfigError):
        load_encryption_keys()


def test_from_environment_uses_keyring(monkeypatch):
    keys = {"v1": os.urandom(32), "v2": os.urandom(32)}
    key_env(monkeypatch, {"v1": b64(keys["v1"]), "v2": b64(keys["v2"])}, "v2")

    encryptor = CredentialEncryptor.from_environment()

    assert encryptor.active_kid == "v2"


# -- multi-kid encrypt/decrypt --------------------------------------------------------


def test_new_writes_use_active_kid():
    encryptor = CredentialEncryptor.from_keys(
        {"old": os.urandom(32), "new": os.urandom(32)}, active_kid="new"
    )
    envelope = encryptor.encrypt_payload(PAYLOAD)
    assert envelope["kid"] == "new"
    assert encryptor.decrypt_payload(envelope) == PAYLOAD


def test_old_kid_envelopes_still_decrypt_after_rotation():
    """Simulates rotation: envelopes written under the old kid remain
    readable through the rotated keyring."""
    old_key, new_key = os.urandom(32), os.urandom(32)
    pre_rotation = CredentialEncryptor(old_key)  # kid=default
    envelope = pre_rotation.encrypt_payload(PAYLOAD)

    rotated = CredentialEncryptor.from_keys(
        {"default": old_key, "v2": new_key}, active_kid="v2"
    )
    assert rotated.decrypt_payload(envelope) == PAYLOAD  # old kid readable

    fresh = rotated.encrypt_payload(PAYLOAD)
    assert fresh["kid"] == "v2"  # new writes use active kid
    assert fresh["ciphertext"] != envelope["ciphertext"]


def test_unknown_kid_fails_closed():
    encryptor = CredentialEncryptor.from_keys(
        {"v1": os.urandom(32)}, active_kid="v1"
    )
    envelope = encryptor.encrypt_payload(PAYLOAD)
    envelope["kid"] = "ghost-kid"

    with pytest.raises(CredentialDecryptionError):
        encryptor.decrypt_payload(envelope)


def test_wrong_key_for_kid_fails_closed():
    writer = CredentialEncryptor.from_keys(
        {"v1": os.urandom(32)}, active_kid="v1"
    )
    envelope = writer.encrypt_payload(PAYLOAD)

    reader = CredentialEncryptor.from_keys(
        {"v1": os.urandom(32)}, active_kid="v1"  # same kid, different key
    )
    with pytest.raises(CredentialDecryptionError):
        reader.decrypt_payload(envelope)


def test_key_material_never_in_repr_or_errors():
    key = os.urandom(32)
    encryptor = CredentialEncryptor.from_keys({"v1": key}, active_kid="v1")
    envelope = encryptor.encrypt_payload(PAYLOAD)
    envelope["kid"] = "ghost"

    with pytest.raises(CredentialDecryptionError) as exc_info:
        encryptor.decrypt_payload(envelope)

    rendered_key = b64(key)
    assert rendered_key not in str(exc_info.value)
    assert rendered_key not in repr(encryptor)
    assert rendered_key not in str(envelope)


def test_aad_binding_across_kids():
    """Re-encryption under a new kid keeps the same AAD semantics."""
    encryptor = CredentialEncryptor.from_keys(
        {"old": os.urandom(32), "new": os.urandom(32)}, active_kid="old"
    )
    envelope = encryptor.encrypt_payload(PAYLOAD, associated_data=b"aad-1")

    encryptor._active_kid = "new"  # simulate rotation within one keyring
    with pytest.raises(CredentialDecryptionError):
        encryptor.decrypt_payload(envelope, associated_data=b"aad-2")
    assert encryptor.decrypt_payload(envelope, associated_data=b"aad-1") == PAYLOAD
