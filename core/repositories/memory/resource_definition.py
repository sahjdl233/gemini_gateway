"""In-memory resource definition repository (CONFIG/R-5 Part D)."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from core.repositories.resource_definition import (
    ResourceDefinition,
    ResourceDefinitionRepository,
)

__all__ = ["MemoryResourceDefinitionRepository"]


class MemoryResourceDefinitionRepository(ResourceDefinitionRepository):
    """Dict-backed definition repository, keyed by (provider, resource_id)."""

    def __init__(self) -> None:
        self._definitions: Dict[
            Tuple[str, str], ResourceDefinition
        ] = {}

    async def list_all(self) -> List[ResourceDefinition]:
        return [
            self._definitions[key]
            for key in sorted(self._definitions)
        ]

    async def get(
        self, provider: str, resource_id: str
    ) -> Optional[ResourceDefinition]:
        return self._definitions.get((provider, resource_id))

    async def save(self, definition: ResourceDefinition) -> None:
        self._definitions[(definition.provider, definition.id)] = definition

    async def delete(self, provider: str, resource_id: str) -> None:
        self._definitions.pop((provider, resource_id), None)
