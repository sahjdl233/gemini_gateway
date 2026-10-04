"""Resource definition repository protocol (CONFIG/R-5 Part A).

Persistence contract for ``ResourceDefinition`` DTOs — the durable layer
owns exactly what docs/ADR-CONFIG-R4-PERSISTENCE-BOUNDARY.md §1 assigns
to the definition layer: provider, resource id, enabled, credential_id
REFERENCE and provider-specific non-secret config.

Forbidden by the contract (and unrepresentable through this interface):

* runtime state (health / cooldown / counters) — that is
  :class:`core.repositories.runtime_state.RuntimeStateStore`'s concern;
* credential resolution — a definition repository hands back the
  reference string and nothing more.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from core.resource_definition import ResourceDefinitionBase

#: The definition DTO managed by this repository.  Alias of the existing
#: pydantic model — the persistence layer does not duplicate the schema.
ResourceDefinition = ResourceDefinitionBase

__all__ = ["ResourceDefinition", "ResourceDefinitionRepository"]


@runtime_checkable
class ResourceDefinitionRepository(Protocol):
    """Async CRUD over resource definitions, keyed by (provider, id)."""

    async def list_all(self) -> list[ResourceDefinition]:
        """All definitions, deterministic order (provider, id)."""
        ...

    async def get(
        self, provider: str, resource_id: str
    ) -> Optional[ResourceDefinition]:
        """The definition for the composite key, or None."""
        ...

    async def save(self, definition: ResourceDefinition) -> None:
        """Insert or replace the definition for its composite key."""
        ...

    async def delete(self, provider: str, resource_id: str) -> None:
        """Remove the definition; unknown keys are a no-op."""
        ...
