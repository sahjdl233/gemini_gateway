"""ResourceDefinition Repository contract (DB-RESOURCE-001-2).

Abstract interface for persisting :class:`ResourceDefinitionBase` instances
and their provider-specific subtypes.  Mirrors the pattern established by
:class:`core.credential.CredentialRepository` (TASK-AUTH-008 / AUTH-009):

* Identity is the composite ``(provider, id)`` pair — never a
  globally unique ``id``.
* Repository input/output uses ``ResourceDefinition`` DTOs only; runtime
  ``Resource`` (with scheduling state, health counters, etc.) is never
  accepted or returned.
* The repository does **not** resolve ``credential_id`` references, does
  not touch credential secrets, and does not fill DTO defaults — callers
  must validate and complete the DTO before passing it in.
* Typed errors carry the composite identity so call sites can distinguish
  duplicates from other conflicts.

Design baseline: ``docs/DB-RESOURCE-DESIGN-001.md`` (§4).

PostgreSQL implementation, startup bootstrap, YAML import/export, Admin
API integration, and runtime reload are all out of scope for this module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from core.resource_definition import (
    ResourceDefinitionBase,
    ResourceDefinitionError,
)


class ResourceDefinitionNotFoundError(ResourceDefinitionError, KeyError):
    """Raised when a resource definition does not exist in the store."""


class DuplicateResourceDefinitionError(ResourceDefinitionError):
    """Raised when an ``add`` or ``update`` violates the composite unique
    constraint on ``(provider, id)``."""


class ResourceRepository(ABC):
    """Contract for persisting and retrieving :class:`ResourceDefinitionBase`
    instances.

    All methods are asynchronous to allow future PostgreSQL and other
    durable implementations without changing the contract.
    """

    @abstractmethod
    async def add(
        self, definition: ResourceDefinitionBase
    ) -> ResourceDefinitionBase:
        """Persist a new resource definition.

        Raises:
            DuplicateResourceDefinitionError: if a definition with the same
                ``(provider, id)`` composite key already exists.
        """

    @abstractmethod
    async def get(
        self, provider: str, resource_id: str
    ) -> Optional[ResourceDefinitionBase]:
        """Return the definition for the given composite key, or ``None``
        if no such definition exists.
        """

    @abstractmethod
    async def require(
        self, provider: str, resource_id: str
    ) -> ResourceDefinitionBase:
        """Return the definition for the given composite key.

        Raises:
            ResourceDefinitionNotFoundError: if no such definition exists.
        """

    @abstractmethod
    async def list(
        self, *, provider: Optional[str] = None
    ) -> List[ResourceDefinitionBase]:
        """Return all definitions, optionally filtered by ``provider``.

        Ordering is deterministic and guaranteed to be ascending by the
        composite key ``(provider, id)``.  When ``provider`` is
        omitted all definitions are returned; when provided only definitions
        for that provider are returned, still sorted ascending by
        ``id`` within that provider.
        """

    @abstractmethod
    async def update(
        self, definition: ResourceDefinitionBase
    ) -> ResourceDefinitionBase:
        """Fully replace the existing definition for the composite key
        contained in ``definition``.

        This is a full replacement — partial updates are not supported.
        The repository must not fall back to an insert (upsert) when the
        definition is absent; callers must use ``require`` first if they
        need to distinguish between "replace existing" and "insert new".

        Raises:
            ResourceDefinitionNotFoundError: if no definition with the same
                ``(provider, id)`` composite key exists.
        """

    @abstractmethod
    async def delete(
        self, provider: str, resource_id: str
    ) -> None:
        """Delete the definition for the given composite key.

        This operation is idempotent: deleting a non-existent definition
        is a no-op and does not raise.
        """
