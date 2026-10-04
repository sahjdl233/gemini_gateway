"""Runtime state store protocol (CONFIG/R-5 Part C).

Persistence contract for discardable scheduling state, keyed by
:class:`core.resource.ResourceKey`.  Mirrors the fields the pool
preserves per ResourceKey (TASK-STATE-001 / CONTROL-007-DECISION-001) —
observability counters and scheduling state travel together here; the
rebind reset policy stays a pool-level decision, not a storage one.

Contract: runtime state is OPTIONAL.  A missing store, or a missing
entry, must never affect startup — definitions alone fully determine
the runtime.  Everything in here is reconstructable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol, runtime_checkable

from core.health import HealthState
from core.resource import ResourceKey

__all__ = ["RuntimeState", "RuntimeStateStore"]


@dataclass(frozen=True)
class RuntimeState:
    """Discardable scheduling state for one ResourceKey."""

    resource_key: ResourceKey
    health: HealthState = HealthState.HEALTHY
    cooldown_until: Optional[datetime] = None
    consecutive_failures: int = 0
    total_requests: int = 0
    total_failures: int = 0


@runtime_checkable
class RuntimeStateStore(Protocol):
    """Async state access, keyed by the composite resource identity."""

    async def get(self, resource_key: ResourceKey) -> Optional[RuntimeState]:
        """The stored state for the key, or None when absent."""
        ...

    async def save(self, state: RuntimeState) -> None:
        """Persist state for ``state.resource_key``."""
        ...
