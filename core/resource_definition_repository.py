"""ResourceDefinition read-side repository abstraction (DB-RESOURCE-004).

A minimal, read-only Protocol through which consumers (future runtime
build, bootstrap reporting, CLI) obtain
:class:`core.resource_definition.ResourceDefinitionBase` DTOs without
knowing — or caring — whether they came from YAML, an in-memory seed, or
PostgreSQL.

Relationship to the DB-RESOURCE-001-2 ``ResourceRepository`` contract:

* That contract is the **durable CRUD** boundary (add/get/require/list/
  update/delete, async, PostgreSQL-first).  It stays unchanged.
* This Protocol is the **definition source** boundary: read-only, no
  persistence semantics, no error contract beyond "missing key → None".
* Identity is the same composite ``(provider, resource_id)`` pair; the
  provider discriminator always travels with the DTO, so consumers never
  re-derive provider from anything else.

Duplicate-identity policy is explicit and lives with the *store*, not
the reader: :class:`MemoryResourceDefinitionRepository` rejects duplicate
composite identities at construction with
:class:`core.resource_repository.DuplicateResourceDefinitionError` (the
YAML loader deliberately passes duplicates through — see
``core.resource_definition_loader`` — so a store is the first layer that
can enforce uniqueness).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Protocol, Sequence, Tuple

from core.resource_definition import ResourceDefinitionBase
from core.resource_repository import DuplicateResourceDefinitionError

__all__ = [
    "ResourceDefinitionRepository",
    "MemoryResourceDefinitionRepository",
]


class ResourceDefinitionRepository(Protocol):
    """Read-only access to resource definitions, source-agnostic."""

    async def list_definitions(self) -> List[ResourceDefinitionBase]:
        """Return all definitions, deterministically ordered ascending by
        ``(provider, resource_id)``."""
        ...

    async def get_definition(
        self, provider: str, id: str
    ) -> Optional[ResourceDefinitionBase]:
        """Return the definition for the composite key, or ``None`` when
        no such definition exists."""
        ...


class MemoryResourceDefinitionRepository:
    """In-memory :class:`ResourceDefinitionRepository`.

    Seeds from an iterable of DTOs (e.g. the YAML loader's output).  The
    composite ``(provider, resource_id)`` key is enforced eagerly: a seed
    containing the same identity twice raises
    ``DuplicateResourceDefinitionError`` before anything is stored — the
    duplicate-identity behaviour is explicit, not "last one wins".
    """

    def __init__(
        self,
        definitions: Sequence[ResourceDefinitionBase] = (),
    ) -> None:
        self._definitions: Dict[Tuple[str, str], ResourceDefinitionBase] = {}
        for definition in definitions:
            key = (definition.provider, definition.id)
            if key in self._definitions:
                raise DuplicateResourceDefinitionError(
                    definition.provider,
                    definition.id,
                    f"duplicate resource definition: "
                    f"provider={definition.provider!r}, "
                    f"id={definition.id!r}",
                )
            self._definitions[key] = definition

    async def list_definitions(self) -> List[ResourceDefinitionBase]:
        return [
            self._definitions[key]
            for key in sorted(self._definitions)
        ]

    async def get_definition(
        self, provider: str, id: str
    ) -> Optional[ResourceDefinitionBase]:
        return self._definitions.get((provider, id))
