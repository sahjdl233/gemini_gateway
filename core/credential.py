"""Credential domain model (TASK-AUTH-002).

A Credential is the first-class owner of long-lived authentication
material (refresh tokens, client secrets, API keys).  Resources reference
a Credential by id instead of embedding the material themselves::

    Resource
      └── credential_id ──→ Credential
                              ├── id
                              ├── type: none | api_key | oauth
                              ├── payload (provider-specific material)
                              ├── created_at
                              └── updated_at

Boundary rules frozen by AUTH-002:

* ``type`` classifies the *lifecycle / handling* of the material, never
  the provider.  There is deliberately no ``gemini_cli`` / ``firebase``
  credential type.
* A Credential is NOT a runtime token cache.  Short-lived access tokens
  and App Check JWTs stay in provider-owned runtime state and must not be
  persisted as Credential payload unless they are genuinely long-lived
  material (e.g. antigravity currently has no refresh loop, so its
  access token is still durable material until AUTH-006).
* Cardinality: ``Resource → 0..1 Credential`` and ``Credential → 0..N
  Resources``.  N:N is intentionally not modelled.
* No encryption and no external persistence here (AUTH-007 / AUTH-008).
  The store below is an in-memory registry so that a future persistence
  layer can replace the storage without touching the domain model.

Security: ``repr`` / ``str`` and ``redacted_dict`` never reveal secret
payload values.  Any payload key whose name looks sensitive (token,
secret, key, password, cookie) is masked.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CredentialType(str, Enum):
    """Lifecycle / handling class of the credential material.

    Not a provider name: both gemini_cli and antigravity are ``oauth``,
    firebase is ``api_key``, fake and anonymous_vertex need no Credential
    at all (or an explicit ``none``).
    """

    NONE = "none"
    API_KEY = "api_key"
    OAUTH = "oauth"


# Substrings that mark a payload value as secret.  Matched against the
# lower-cased key name, so unknown-but-secret-shaped keys are masked too.
_SECRET_SUBSTRINGS = ("token", "secret", "password", "cookie", "key")


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(mark in lowered for mark in _SECRET_SUBSTRINGS)


def redact_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``payload`` with secret-looking values masked."""
    return {
        key: ("***" if _is_secret_key(key) and value else value)
        for key, value in payload.items()
    }


class Credential(BaseModel):
    """First-class holder of long-lived authentication material."""

    id: str
    type: CredentialType = CredentialType.NONE
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def redacted_payload(self) -> Dict[str, Any]:
        """Payload view safe for logs / API responses."""
        return redact_payload(self.payload)

    def redacted_dict(self) -> Dict[str, Any]:
        """Config-like view with all secret material masked."""
        return {
            "id": self.id,
            "type": self.type.value,
            "payload": self.redacted_payload(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    def __repr__(self) -> str:
        return (
            f"Credential(id={self.id!r}, type={self.type.value!r}, "
            f"payload={self.redacted_payload()!r})"
        )

    def __str__(self) -> str:
        return repr(self)


class DuplicateCredentialError(ValueError):
    """Raised when a credential id is registered twice."""


class UnknownCredentialError(KeyError):
    """Raised when a credential id is required but not registered."""


class CredentialRepository(ABC):
    """Stable credential persistence contract (TASK-AUTH-008).

    The seam between the Credential domain and future durable
    persistence (AUTH-009: PostgreSQL / Supabase)::

        Credential
            ↓
        CredentialRepository            (this contract)
            ↓
        CredentialStore (in-memory)     ← current implementation
        PostgresCredentialRepository    ← AUTH-009, unchanged providers

    Contract semantics (frozen so a durable implementation can drop in
    without touching Provider / AuthAdapter code):

    * ``add`` rejects duplicate ids with DuplicateCredentialError.
    * ``get`` returns the credential or ``None`` for unknown ids.
    * ``require`` returns the credential or raises
      UnknownCredentialError.
    * ``update_payload`` requires the credential to exist
      (UnknownCredentialError otherwise) and replaces the payload
      atomically, bumping ``updated_at``.
    * ``remove`` is idempotent for unknown ids.
    * ``list`` returns all credentials in insertion order.
    * Repositories persist only the durable Credential shape
      (id / type / payload / timestamps); runtime auth state never
      enters this contract.  Encryption of payloads at rest is a
      repository-implementation concern (AUTH-007 primitive), invisible
      to callers.
    """

    @abstractmethod
    def add(self, credential: Credential) -> Credential:
        """Register a credential; DuplicateCredentialError on reuse."""
        ...

    @abstractmethod
    def get(self, credential_id: str) -> Optional[Credential]:
        """Return the credential or None when unknown."""
        ...

    @abstractmethod
    def require(self, credential_id: str) -> Credential:
        """Return the credential or raise UnknownCredentialError."""
        ...

    @abstractmethod
    def update_payload(
        self,
        credential_id: str,
        payload: Dict[str, Any],
    ) -> Credential:
        """Replace the payload (must exist) and bump updated_at."""
        ...

    @abstractmethod
    def remove(self, credential_id: str) -> None:
        """Drop a credential; unknown ids are ignored (idempotent)."""
        ...

    @abstractmethod
    def list(self) -> List[Credential]:
        """All registered credentials (insertion order)."""
        ...

    def __contains__(self, credential_id: object) -> bool:
        return self.get(str(credential_id)) is not None


class CredentialStore(CredentialRepository):
    """In-memory credential repository (AUTH-002 v1, AUTH-008 contract).

    Deliberately minimal: lookup + CRUD is all providers need to resolve
    ``resource.credential_id``.  Encryption and durable persistence are
    AUTH-007 / AUTH-008+ concerns; this in-memory implementation is the
    seam a durable repository replaces.
    All operations are synchronous dict operations (safe under asyncio's
    single-threaded event loop).
    """

    def __init__(self) -> None:
        self._credentials: Dict[str, Credential] = {}

    def add(self, credential: Credential) -> Credential:
        """Register a credential; rejects duplicate ids."""
        if credential.id in self._credentials:
            raise DuplicateCredentialError(
                f"credential '{credential.id}' already registered"
            )
        self._credentials[credential.id] = credential
        return credential

    def get(self, credential_id: str) -> Optional[Credential]:
        """Return the credential or None when unknown."""
        return self._credentials.get(credential_id)

    def require(self, credential_id: str) -> Credential:
        """Return the credential or raise UnknownCredentialError."""
        credential = self._credentials.get(credential_id)
        if credential is None:
            raise UnknownCredentialError(
                f"credential '{credential_id}' is not registered"
            )
        return credential

    def update_payload(
        self,
        credential_id: str,
        payload: Dict[str, Any],
    ) -> Credential:
        """Replace the payload and bump ``updated_at``."""
        credential = self.require(credential_id)
        credential.payload = dict(payload)
        credential.updated_at = utcnow()
        return credential

    def remove(self, credential_id: str) -> None:
        """Drop a credential. Unknown ids are ignored (idempotent)."""
        self._credentials.pop(credential_id, None)

    def list(self) -> List[Credential]:
        """All registered credentials (unsorted; insertion order)."""
        return list(self._credentials.values())

    def __contains__(self, credential_id: object) -> bool:
        return credential_id in self._credentials

    def __len__(self) -> int:
        return len(self._credentials)
