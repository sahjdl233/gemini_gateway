"""In-memory credential repository (CONFIG/R-5 Part D)."""

from __future__ import annotations

from typing import Dict, Optional

from core.repositories.credential import (
    CredentialMaterial,
    CredentialRepository,
)

__all__ = ["MemoryCredentialRepository"]


class MemoryCredentialRepository(CredentialRepository):
    """Dict-backed secret-material store.

    Isolation semantics: the stored payload is copied on save and the
    returned material's payload is copied on get, so callers can never
    alias each other's material through this layer.
    """

    def __init__(self) -> None:
        self._materials: Dict[str, CredentialMaterial] = {}

    async def get_secret(
        self, credential_id: str
    ) -> Optional[CredentialMaterial]:
        material = self._materials.get(credential_id)
        if material is None:
            return None
        return CredentialMaterial(
            type=material.type, payload=dict(material.payload)
        )

    async def save_secret(
        self, credential_id: str, material: CredentialMaterial
    ) -> None:
        self._materials[credential_id] = CredentialMaterial(
            type=material.type, payload=dict(material.payload)
        )
