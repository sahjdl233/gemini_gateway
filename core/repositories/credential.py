"""Credential repository protocol (CONFIG/R-5 Part B).

Persistence contract for secret material.  Callers see
:class:`CredentialMaterial` in and out — never storage, never the
existing :class:`core.credential.Credential` aggregate (which carries
identity/lifecycle metadata the persistence layer does not need).

Boundary (ADR-CONFIG-R4 §2): the definition layer resolves nothing; the
ONLY way runtime code obtains material is through this repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

__all__ = ["CredentialMaterial", "CredentialRepository"]


@dataclass(frozen=True)
class CredentialMaterial:
    """Opaque secret material for one credential.

    ``type`` mirrors the credential lifecycle class (see
    core.credential.CredentialType); ``payload`` is the provider-shaped
    material mapping.  Treated as an opaque value object here — this
    layer never interprets keys.
    """

    type: str
    payload: dict = field(default_factory=dict)


@runtime_checkable
class CredentialRepository(Protocol):
    """Async secret-material access, keyed by credential id."""

    async def get_secret(
        self, credential_id: str
    ) -> Optional[CredentialMaterial]:
        """The material for the credential, or None when unknown."""
        ...

    async def save_secret(
        self, credential_id: str, material: CredentialMaterial
    ) -> None:
        """Insert or replace the material for the credential."""
        ...
