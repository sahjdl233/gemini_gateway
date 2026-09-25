"""Credential encryption boundary (TASK-AUTH-007).

An infrastructure-level, provider-agnostic boundary that converts
Credential durable material between in-memory plaintext and an encrypted,
versioned envelope suitable for a future persistence layer (AUTH-008 /
AUTH-009)::

    Credential.payload (plaintext, memory only)
        │  canonical JSON → UTF-8
        ▼
    AES-256-GCM (random 12-byte nonce per encryption)
        ▼
    envelope {v, alg, kid, nonce, ciphertext}   (metadata is plaintext)

Boundary rules frozen by AUTH-007:

* Independent of the Credential domain: ``Credential`` never encrypts,
  decrypts or loads keys.  Independent of Providers: no adapter imports
  this module; Scheduler / ResourcePool / ExecutionBackend never learn
  encryption exists.
* Runtime state (access tokens, App Check JWTs, runtime expiry, rotated
  refresh tokens, failure counters) never enters this path.  The caller
  decides what "durable material" is — that responsibility lives in the
  ProviderAuthAdapters (AUTH-004/005/006), not here.
* Fail closed: every failure mode (missing/invalid key, malformed or
  tampered envelope, unknown version/algorithm, wrong key) raises a
  dedicated error.  There is NO plaintext fallback — decrypting a value
  that is not a valid envelope raises, it never "returns the input".
* No key persistence, no default key, no secret logging: the master key
  comes exclusively from ``GEMINI_GATEWAY_ENCRYPTION_KEY`` (base64 of
  exactly 32 raw bytes) and never appears in envelopes, logs, exception
  messages or reprs.

Envelope format (v1)::

    {
      "v": 1,                        # envelope version (int)
      "alg": "AES-256-GCM",          # algorithm (str)
      "kid": "default",              # key id, reserved for rotation (str)
      "nonce": "<base64>",           # 12-byte AES-GCM nonce (str)
      "ciphertext": "<base64>"       # ciphertext + GCM tag (str)
    }

``kid`` is recorded but only ``"default"`` is accepted in AUTH-007 —
multi-key rotation arrives later (AUTH-008+).
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Dict, Mapping, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Envelope metadata (not secrets).
ENVELOPE_VERSION = 1
ENVELOPE_ALGORITHM = "AES-256-GCM"
ENVELOPE_KEY_ID = "default"

# Master key configuration.
ENCRYPTION_KEY_ENV_VAR = "GEMINI_GATEWAY_ENCRYPTION_KEY"
KEY_BYTES = 32  # AES-256
NONCE_BYTES = 12  # AES-GCM standard nonce size


class CredentialEncryptionError(Exception):
    """Base for encryption boundary failures.

    Message hygiene: never includes the key, plaintext payload or
    ciphertext material.
    """


class CredentialEncryptionConfigError(CredentialEncryptionError):
    """Master key configuration failure (missing env var, invalid base64,
    wrong decoded length).  Explicit by design: no default key is ever
    generated, because a silently-different key would make every existing
    credential unrecoverable after a restart."""


class CredentialDecryptionError(CredentialEncryptionError):
    """Decryption/integrity/envelope failure: unknown version or
    algorithm, malformed or missing envelope fields, invalid encodings,
    tampered ciphertext/nonce, wrong key.  Never falls back to plaintext."""


def load_encryption_key() -> bytes:
    """Load the master key from ``GEMINI_GATEWAY_ENCRYPTION_KEY``.

    The value must be standard base64 encoding of exactly 32 raw bytes
    (AES-256).  Any deviation is an explicit configuration error — the
    key is never defaulted, derived, truncated or padded.
    """
    raw = os.environ.get(ENCRYPTION_KEY_ENV_VAR)
    if not raw:
        raise CredentialEncryptionConfigError(
            f"environment variable {ENCRYPTION_KEY_ENV_VAR} is not set; "
            "credential encryption requires an explicit master key "
            "(base64 of 32 bytes)"
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError) as exc:
        raise CredentialEncryptionConfigError(
            f"{ENCRYPTION_KEY_ENV_VAR} is not valid base64"
        ) from exc
    if len(key) != KEY_BYTES:
        raise CredentialEncryptionConfigError(
            f"{ENCRYPTION_KEY_ENV_VAR} must decode to exactly "
            f"{KEY_BYTES} bytes (AES-256), got {len(key)}"
        )
    return key


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    """Deterministic JSON serialization (no pickle, no code execution)."""
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CredentialEncryptionError(
            "credential payload is not JSON-serializable"
        ) from exc


def _b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64decode_str(value: Any, field: str) -> bytes:
    """Decode a base64 envelope field with explicit failure semantics."""
    if not isinstance(value, str):
        raise CredentialDecryptionError(
            f"envelope field '{field}' must be a base64 string"
        )
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise CredentialDecryptionError(
            f"envelope field '{field}' is not valid base64"
        ) from exc


def _require_field(envelope: Any, field: str, expected_type: type) -> Any:
    """Read and type-check one envelope field (bool never passes as int)."""
    if not isinstance(envelope, dict):
        raise CredentialDecryptionError("envelope must be a JSON object")
    if field not in envelope:
        raise CredentialDecryptionError(f"envelope field '{field}' is missing")
    value = envelope[field]
    if expected_type is not bool and isinstance(value, bool):
        raise CredentialDecryptionError(
            f"envelope field '{field}' has invalid type"
        )
    if not isinstance(value, expected_type):
        raise CredentialDecryptionError(
            f"envelope field '{field}' has invalid type"
        )
    return value


class CredentialEncryptor:
    """AES-256-GCM encryptor for Credential durable material.

    Provider-agnostic infrastructure boundary: callers pass a JSON-
    compatible payload mapping; the encryptor owns canonical
    serialization, nonce generation, the versioned envelope and
    fail-closed error semantics.
    """

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:
            raise CredentialEncryptionConfigError(
                f"encryption key must be exactly {KEY_BYTES} bytes"
            )
        self._aesgcm = AESGCM(bytes(key))

    @classmethod
    def from_environment(cls) -> "CredentialEncryptor":
        """Build an encryptor using the configured master key."""
        return cls(load_encryption_key())

    def encrypt_payload(
        self,
        payload: Mapping[str, Any],
        *,
        associated_data: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """Encrypt a payload mapping into a versioned envelope.

        A cryptographically random 12-byte nonce is generated for every
        call (never reused across encryptions).
        """
        plaintext = _canonical_json(payload)
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext, associated_data)
        return {
            "v": ENVELOPE_VERSION,
            "alg": ENVELOPE_ALGORITHM,
            "kid": ENVELOPE_KEY_ID,
            "nonce": _b64encode(nonce),
            "ciphertext": _b64encode(ciphertext),
        }

    def decrypt_payload(
        self,
        envelope: Any,
        *,
        associated_data: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """Decrypt a versioned envelope back into the payload mapping.

        Fail closed: malformed envelopes, unknown versions/algorithms,
        invalid encodings and any integrity failure (wrong key, tampered
        ciphertext/nonce, invalid tag) raise
        :class:`CredentialDecryptionError`.  Plaintext input is never
        treated as a valid envelope.
        """
        version = _require_field(envelope, "v", int)
        algorithm = _require_field(envelope, "alg", str)
        key_id = _require_field(envelope, "kid", str)
        nonce = _b64decode_str(
            _require_field(envelope, "nonce", str), "nonce"
        )
        ciphertext = _b64decode_str(
            _require_field(envelope, "ciphertext", str), "ciphertext"
        )
        if version != ENVELOPE_VERSION:
            raise CredentialDecryptionError(
                f"unsupported envelope version: {version}"
            )
        if algorithm != ENVELOPE_ALGORITHM:
            raise CredentialDecryptionError(
                f"unsupported envelope algorithm: {algorithm}"
            )
        if key_id != ENVELOPE_KEY_ID:
            raise CredentialDecryptionError(
                f"unknown key id: {key_id}"
            )
        if len(nonce) != NONCE_BYTES:
            raise CredentialDecryptionError(
                "envelope field 'nonce' has invalid length"
            )
        try:
            plaintext = self._aesgcm.decrypt(nonce, ciphertext, associated_data)
        except InvalidTag as exc:
            raise CredentialDecryptionError(
                "decryption failed: integrity check did not pass "
                "(wrong key or tampered data)"
            ) from exc
        try:
            payload = json.loads(plaintext.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CredentialDecryptionError(
                "decrypted payload is not valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise CredentialDecryptionError(
                "decrypted payload must be a JSON object"
            )
        return payload
