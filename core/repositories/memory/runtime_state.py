"""In-memory runtime state store (CONFIG/R-5 Part D)."""

from __future__ import annotations

from typing import Dict, Optional

from core.resource import ResourceKey
from core.repositories.runtime_state import RuntimeState, RuntimeStateStore

__all__ = ["MemoryRuntimeStateStore"]


class MemoryRuntimeStateStore(RuntimeStateStore):
    """Dict-backed runtime state, keyed by ResourceKey.

    Optional by contract: an absent key returns None and consumes
    nothing — startup never depends on this store having content.
    """

    def __init__(self) -> None:
        self._states: Dict[ResourceKey, RuntimeState] = {}

    async def get(self, resource_key: ResourceKey) -> Optional[RuntimeState]:
        return self._states.get(resource_key)

    async def save(self, state: RuntimeState) -> None:
        self._states[state.resource_key] = state
