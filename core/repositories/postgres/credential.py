"""PostgreSQL credential repository facade (CONFIG/R-6).

Async ``CredentialRepository`` protocol adapter over the existing AUTH-009
sync ``PostgreSQLCredentialRepository``: same ``credentials`` table, same
``CredentialEncryptor`` — no new crypto design.  The sync delegation is
deliberate: the application's credential store is synchronous everywhere
(routes and adapters already call it from async contexts).
"""

from __future__ import annotations

from typing import Optional

from core.credential import Credential, CredentialType
from core.credential_postgres import PostgreSQLCredentialRepository
from core.repositories.credential import (
    CredentialMaterial,
    CredentialRepository,
)

__all__ = ["PostgresCredentialRepository"]


class PostgresCredentialRepository(CredentialRepository):
    """``CredentialRepository`` protocol facade over the AUTH-009 store."""

    def __init__(self, inner: PostgreSQLCredentialRepository) -> None:
        self._inner = inner

    @staticmethod
    def _to_material(credential: Credential) -> CredentialMaterial:
        return CredentialMaterial(
            type=credential.type.value, payload=dict(credential.payload)
        )

    async def get_secret(
        self, credential_id: str
    ) -> Optional[CredentialMaterial]:
        credential = self._inner.get(credential_id)
        if credential is None:
            return None
        return self._to_material(credential)

    async def save_secret(
        self, credential_id: str, material: CredentialMaterial
    ) -> None:
        new_type = CredentialType(material.type)
        payload = dict(material.payload)
        existing = self._inner.get(credential_id)
        if existing is None:
            self._inner.add(
                Credential(id=credential_id, type=new_type, payload=payload)
            )
            return
        if existing.type is not new_type:
            # The lifecycle class changed: replace the row outright —
            # update_payload only rewrites the payload.
            self._inner.remove(credential_id)
            self._inner.add(
                Credential(id=credential_id, type=new_type, payload=payload)
            )
            return
        self._inner.update_payload(credential_id, payload)
