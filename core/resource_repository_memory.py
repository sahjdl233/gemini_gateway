"""In-memory ResourceRepository implementation (DB-RESOURCE-006).

The durable-side **sink** for resource bootstrap until a PostgreSQL
resource store is deployed: a plain dict keyed by the composite
``(provider, resource_id)`` with the frozen DB-RESOURCE-001-2 semantics —

* ``add`` → ``DuplicateResourceDefinitionError`` on an existing key;
* ``get`` → ``None`` when missing; ``require``/``update`` →
  ``UnknownResourceDefinitionError``;
* ``update`` is a full replacement, never an upsert;
* ``delete`` is idempotent;
* ``list`` is deterministic ascending by ``(provider, resource_id)``.

No persistence, no connections, no runtime Resource state — DTOs only.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from core.resource_definition import ResourceDefinitionBase
from core.resource_repository import (
    DuplicateResourceDefinitionError,
    ResourceRepository,
    UnknownResourceDefinitionError,
)

__all__ = ["MemoryResourceRepository"]


class MemoryResourceRepository(ResourceRepository):
    """Dict-backed :class:`ResourceRepository` (bootstrap sink / tests)."""

    def __init__(self) -> None:
        self._definitions: Dict[Tuple[str, str], ResourceDefinitionBase] = {}

    async def add(
        self, definition: ResourceDefinitionBase
    ) -> ResourceDefinitionBase:
        key = (definition.provider, definition.id)
        if key in self._definitions:
            raise DuplicateResourceDefinitionError(
                definition.provider,
                definition.id,
                f"resource definition already exists: "
                f"provider={definition.provider!r}, id={definition.id!r}",
            )
        self._definitions[key] = definition
        return definition

    async def get(
        self, provider: str, resource_id: str
    ) -> Optional[ResourceDefinitionBase]:
        return self._definitions.get((provider, resource_id))

    async def require(
        self, provider: str, resource_id: str
    ) -> ResourceDefinitionBase:
        definition = await self.get(provider, resource_id)
        if definition is None:
            raise UnknownResourceDefinitionError(
                provider,
                resource_id,
                f"resource definition not found: provider={provider!r}, "
                f"id={resource_id!r}",
            )
        return definition

    async def list(
        self, *, provider: Optional[str] = None
    ) -> List[ResourceDefinitionBase]:
        items = [
            definition
            for (p, _), definition in sorted(self._definitions.items())
            if provider is None or p == provider
        ]
        return items

    async def update(
        self, definition: ResourceDefinitionBase
    ) -> ResourceDefinitionBase:
        key = (definition.provider, definition.id)
        if key not in self._definitions:
            raise UnknownResourceDefinitionError(
                definition.provider,
                definition.id,
                f"resource definition not found: "
                f"provider={definition.provider!r}, id={definition.id!r}",
            )
        self._definitions[key] = definition
        return definition

    async def delete(self, provider: str, resource_id: str) -> None:
        self._definitions.pop((provider, resource_id), None)
