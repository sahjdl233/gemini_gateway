"""Credential encryption boundary tests (TASK-AUTH-007).

Covers the required cryptography, key handling, envelope validation,
fail-closed semantics and secret-hygiene rules.  Test keys are generated
dynamically with os.urandom and injected via monkeypatch only — no
long-term key is committed and the real environment is never touched.
"""

from __future__ import annotations

import base64
import copy
import os

import pytest

from core.credential_encryption import (
    CredentialDecryptionError,
    CredentialEncryptionConfigError,
    CredentialEncryptionError,
    CredentialEncryptor,
    ENCRYPTION_KEY_ENV_VAR,
    NONCE_BYTES,
    load_encryption_key,
)


def b64_key(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture
def key_raw() -> bytes:
    return os.urandom(32)


@pytest.fixture
def key_b64(key_raw: bytes) -> str:
    return b64_key(key_raw)


@pytest.fixture
def encryptor(key_raw: bytes) -> CredentialEncryptor:
    return CredentialEncryptor(key_raw)


@pytest.fixture
def env_key(monkeypatch, key_b64: str) -> str:
    monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, key_b64)
    return key_b64


SAMPLE_PAYLOAD = {
    "refresh_token": "rt-secret-value",
    "client_id": "client-id-1",
    "client_secret": "cs-secret-value",
    "nested": {"value": 123},
}


# -- 22.1 round trip ------------------------------------------------------------

def test_round_trip_restores_payload(encryptor):
    envelope = encryptor.encrypt_payload(SAMPLE_PAYLOAD)
    assert encryptor.decrypt_payload(envelope) == SAMPLE_PAYLOAD


def test_round_trip_with_associated_data(encryptor):
    envelope = encryptor.encrypt_payload(SAMPLE_PAYLOAD, associated_data=b"context")
    assert (
        encryptor.decrypt_payload(envelope, associated_data=b"context")
        == SAMPLE_PAYLOAD
    )


def test_wrong_associated_data_fails(encryptor):
    envelope = encryptor.encrypt_payload(SAMPLE_PAYLOAD, associated_data=b"context")
    with pytest.raises(CredentialDecryptionError):
        encryptor.decrypt_payload(envelope, associated_data=b"other")


# -- 22.2 empty payload ----------------------------------------------------------

def test_empty_payload_round_trip(encryptor):
    envelope = encryptor.encrypt_payload({})
    assert encryptor.decrypt_payload(envelope) == {}


# -- 22.3 unicode ------------------------------------------------------------------

def test_unicode_payload_round_trip(encryptor):
    payload = {
        "refresh_token": "令牌-🔐-значение",
        "note": "中文 / emoji 🚀 / ±§µ",
    }
    envelope = encryptor.encrypt_payload(payload)
    assert encryptor.decrypt_payload(envelope) == payload


def test_non_json_payload_rejected(encryptor):
    with pytest.raises(CredentialEncryptionError):
        encryptor.encrypt_payload({"bad": object()})


# -- 22.4 non-deterministic nonce ---------------------------------------------------


def test_nonce_is_random_per_encryption(encryptor):
    first = encryptor.encrypt_payload(SAMPLE_PAYLOAD)
    second = encryptor.encrypt_payload(SAMPLE_PAYLOAD)

    assert first["nonce"] != second["nonce"]
    assert first["ciphertext"] != second["ciphertext"]


def test_nonce_length_is_12_bytes(encryptor):
    import base64 as b64

    envelope = encryptor.encrypt_payload(SAMPLE_PAYLOAD)
    assert len(b64.b64decode(envelope["nonce"])) == NONCE_BYTES


# -- envelope structure ----------------------------------------------------------------


def test_envelope_carries_version_algorithm_keyid(encryptor):
    envelope = encryptor.encrypt_payload(SAMPLE_PAYLOAD)
    assert set(envelope) == {"v", "alg", "kid", "nonce", "ciphertext"}
    assert envelope["v"] == 1
    assert envelope["alg"] == "AES-256-GCM"
    assert envelope["kid"] == "default"


def test_envelope_metadata_is_not_plaintext_secret(encryptor):
    envelope = encryptor.encrypt_payload(SAMPLE_PAYLOAD)
    rendered = str(envelope)
    # base64 ciphertext/nonce only; raw secrets must not appear anywhere
    assert "rt-secret-value" not in rendered
    assert "cs-secret-value" not in rendered


# -- 22.5/22.6 tamper detection ----------------------------------------------------------


def test_ciphertext_tampering_fails(encryptor):
    envelope = encryptor.encrypt_payload(SAMPLE_PAYLOAD)
    raw = bytearray(base64.b64decode(envelope["ciphertext"]))
    raw[0] ^= 0x01
    envelope["ciphertext"] = base64.b64encode(bytes(raw)).decode("ascii")

    with pytest.raises(CredentialDecryptionError):
        encryptor.decrypt_payload(envelope)


def test_nonce_tampering_fails(encryptor):
    envelope = encryptor.encrypt_payload(SAMPLE_PAYLOAD)
    raw = bytearray(base64.b64decode(envelope["nonce"]))
    raw[0] ^= 0x01
    envelope["nonce"] = base64.b64encode(bytes(raw)).decode("ascii")

    with pytest.raises(CredentialDecryptionError):
        encryptor.decrypt_payload(envelope)


# -- 22.7 wrong key ------------------------------------------------------------------------


def test_wrong_key_fails(key_raw):
    encryptor_a = CredentialEncryptor(key_raw)
    encryptor_b = CredentialEncryptor(os.urandom(32))

    envelope = encryptor_a.encrypt_payload(SAMPLE_PAYLOAD)

    with pytest.raises(CredentialDecryptionError):
        encryptor_b.decrypt_payload(envelope)


# -- 22.8/22.9 key loading -------------------------------------------------------------------


def test_missing_key_is_explicit_config_error(monkeypatch):
    monkeypatch.delenv(ENCRYPTION_KEY_ENV_VAR, raising=False)
    with pytest.raises(CredentialEncryptionConfigError):
        load_encryption_key()


def test_invalid_base64_key_fails(monkeypatch):
    monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, "not-valid-base64!!!")
    with pytest.raises(CredentialEncryptionConfigError):
        load_encryption_key()


def test_wrong_length_key_fails(monkeypatch):
    monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, b64_key(os.urandom(16)))
    with pytest.raises(CredentialEncryptionConfigError):
        load_encryption_key()


def test_no_default_key_is_generated(monkeypatch):
    monkeypatch.delenv(ENCRYPTION_KEY_ENV_VAR, raising=False)
    with pytest.raises(CredentialEncryptionConfigError):
        CredentialEncryptor.from_environment()


def test_encryptor_rejects_wrong_length_key():
    with pytest.raises(CredentialEncryptionConfigError):
        CredentialEncryptor(os.urandom(16))


# -- 22.10/22.11/22.12 envelope validation ------------------------------------------------------


@pytest.mark.parametrize(
    "broken",
    [
        {},  # entirely empty
        {"alg": "AES-256-GCM", "kid": "default", "nonce": "AAAA", "ciphertext": "AAAA"},  # no v
        {"v": 1, "kid": "default", "nonce": "AAAA", "ciphertext": "AAAA"},  # no alg
        {"v": 1, "alg": "AES-256-GCM", "nonce": "AAAA", "ciphertext": "AAAA"},  # no kid
        {"v": 1, "alg": "AES-256-GCM", "kid": "default", "ciphertext": "AAAA"},  # no nonce
        {"v": 1, "alg": "AES-256-GCM", "kid": "default", "nonce": "AAAA"},  # no ciphertext
        {"v": "1", "alg": "AES-256-GCM", "kid": "default", "nonce": "AAAA", "ciphertext": "AAAA"},  # v wrong type
        {"v": True, "alg": "AES-256-GCM", "kid": "default", "nonce": "AAAA", "ciphertext": "AAAA"},  # bool as int
        {"v": 1, "alg": 1, "kid": "default", "nonce": "AAAA", "ciphertext": "AAAA"},  # alg wrong type
        {"v": 1, "alg": "AES-256-GCM", "kid": "default", "nonce": "not-base64!!", "ciphertext": "AAAA"},
        {"v": 1, "alg": "AES-256-GCM", "kid": "default", "nonce": "AAAA", "ciphertext": "not-base64!!"},
        {"v": 1, "alg": "AES-256-GCM", "kid": "default", "nonce": "", "ciphertext": ""},  # invalid nonce length
        [1, 2, 3],  # not a JSON object
        "plaintext-string",  # not a dict at all
    ],
)
def test_malformed_envelope_fails_closed(encryptor, broken):
    with pytest.raises(CredentialDecryptionError):
        encryptor.decrypt_payload(broken)


def test_unsupported_version_fails(encryptor):
    envelope = encryptor.encrypt_payload(SAMPLE_PAYLOAD)
    envelope["v"] = 999
    with pytest.raises(CredentialDecryptionError):
        encryptor.decrypt_payload(envelope)


def test_unsupported_algorithm_fails(encryptor):
    envelope = encryptor.encrypt_payload(SAMPLE_PAYLOAD)
    envelope["alg"] = "UNKNOWN"
    with pytest.raises(CredentialDecryptionError):
        encryptor.decrypt_payload(envelope)


def test_unknown_key_id_fails(encryptor):
    envelope = encryptor.encrypt_payload(SAMPLE_PAYLOAD)
    envelope["kid"] = "future-key"
    with pytest.raises(CredentialDecryptionError):
        encryptor.decrypt_payload(envelope)


# -- 22.13 no plaintext fallback -------------------------------------------------------------------


def test_plaintext_json_is_not_a_valid_envelope(encryptor):
    import json

    plaintext = json.dumps(SAMPLE_PAYLOAD, sort_keys=True, separators=(",", ":"))
    with pytest.raises(CredentialDecryptionError):
        encryptor.decrypt_payload(plaintext)


def test_raw_ciphertext_bytes_are_not_a_valid_envelope(encryptor):
    envelope = encryptor.encrypt_payload(SAMPLE_PAYLOAD)
    with pytest.raises(CredentialDecryptionError):
        encryptor.decrypt_payload(envelope["ciphertext"])


# -- 22.14 no secret leakage ---------------------------------------------------------------------------


def test_errors_do_not_leak_secrets(encryptor):
    envelope = encryptor.encrypt_payload(SAMPLE_PAYLOAD)

    # tampered ciphertext: the failure message must not echo material
    raw = bytearray(base64.b64decode(envelope["ciphertext"]))
    raw[0] ^= 0x01
    envelope["ciphertext"] = base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(CredentialDecryptionError) as exc_info:
        encryptor.decrypt_payload(envelope)

    message = str(exc_info.value)
    assert "rt-secret-value" not in message
    assert "cs-secret-value" not in message
    assert "client-id-1" not in message  # decrypted plaintext never leaks
    assert envelope["ciphertext"] not in message


def test_key_never_appears_in_envelope_or_errors(monkeypatch, key_b64):
    monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, key_b64)
    encryptor = CredentialEncryptor.from_environment()
    envelope = encryptor.encrypt_payload(SAMPLE_PAYLOAD)

    rendered = str(envelope)
    assert key_b64 not in rendered
    with pytest.raises(CredentialEncryptionError) as exc_info:
        CredentialEncryptor(os.urandom(32)).decrypt_payload(envelope)
    assert key_b64 not in str(exc_info.value)


def test_error_hierarchy_is_explicit():
    assert issubclass(CredentialEncryptionConfigError, CredentialEncryptionError)
    assert issubclass(CredentialDecryptionError, CredentialEncryptionError)


# -- 23 environment isolation ----------------------------------------------------------------------


def test_env_var_is_restored_after_test(monkeypatch, key_b64):
    monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, key_b64)
    assert os.environ[ENCRYPTION_KEY_ENV_VAR] == key_b64
    # monkeypatch undoes this automatically; nothing else is required


def test_payload_not_mutated_by_encryption(encryptor):
    payload = copy.deepcopy(SAMPLE_PAYLOAD)
    encryptor.encrypt_payload(payload)
    assert payload == SAMPLE_PAYLOAD
