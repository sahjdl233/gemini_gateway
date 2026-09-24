"""Resource Pool contract.

Each provider owns its own pool of Resources (FirebasePool, VertexPool,
CLIPool, ...).  TASK-000 ships a generic in-memory pool.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import List, Optional, Set

from .cooldown import CooldownManager
from .errors import ProviderError
from .health import HealthState
from .resource import Resource


class ResourcePool(ABC):
    """Abstract resource-pool contract (TASK-000)."""

    @property
    @abstractmethod
    def resources(self) -> List[Resource]:
        """The resources managed by this pool (read-only view)."""

    @abstractmethod
    async def has_available(self, *, skip: Optional[Set[str]] = None) -> bool:
        """True if at least one eligible resource exists right now."""

    @abstractmethod
    async def acquire(
        self, *, skip: Optional[Set[str]] = None
    ) -> Optional[Resource]:
        """Acquire an eligible resource (health/cooldown aware), or None."""

    @abstractmethod
    async def release(self, resource: Resource) -> None:
        """Return a resource after use."""

    @abstractmethod
    async def record_success(self, resource: Resource) -> None:
        """Record a completed success."""

    @abstractmethod
    async def record_failure(self, resource: Resource, error: ProviderError) -> None:
        """Record a failure (degrades/cooldowns the resource)."""

    @abstractmethod
    async def record_rate_limit(
        self, resource: Resource, retry_after: Optional[float] = None
    ) -> None:
        """Record a 429 (cooldowns the resource, honours Retry-After)."""


class InMemoryPool(ResourcePool):
    """Thread-safe in-memory pool with health/cooldown-aware, low-load
    selection.  Base implementation for TASK-000 and the FakeProvider."""

    def __init__(
        self,
        *,
        provider: str,
        resources: List[Resource],
        cooldown: CooldownManager,
    ) -> None:
        self.provider = provider
        self._resources = list(resources)
        self._cooldown = cooldown
        self._cursor = 0
        self._lock = asyncio.Lock()

    @property
    def resources(self) -> List[Resource]:
        return list(self._resources)

    async def add_resource(self, resource: Resource) -> None:
        """Add a resource to this existing pool at runtime.

        This is intentionally a small concrete-pool management seam; resource
        selection and scheduling semantics remain unchanged.
        """
        async with self._lock:
            if any(existing.resource_key == resource.resource_key for existing in self._resources):
                raise ValueError(f"resource already exists: {resource.id}")
            self._resources.append(resource)

    async def remove_resource(self, resource: Resource) -> None:
        """Remove an idle resource from this pool at runtime."""
        async with self._lock:
            if resource.in_flight:
                raise ValueError(f"resource is in flight: {resource.id}")
            self._resources.remove(resource)

    def _eligible(self, skip: Optional[Set[str]] = None) -> List[Resource]:
        eligible: List[Resource] = []
        for resource in self._resources:
            if not resource.enabled:
                continue
            if self._cooldown.in_cooldown(resource):
                continue
            if resource.health in (HealthState.DISABLED, HealthState.COOLDOWN):
                continue
            if skip and resource.resource_key in skip:
                continue
            eligible.append(resource)
        return eligible

    def _select_min_in_flight(self, eligible: List[Resource]) -> Resource:
        """Select the least-loaded eligible resource.

        The cursor advances after every acquisition, so it only acts as a
        deterministic tie-breaker when multiple resources share the minimum
        in_flight value.
        """
        start = self._cursor % len(eligible)
        min_in_flight = min(res.in_flight for res in eligible)
        for offset in range(len(eligible)):
            resource = eligible[(start + offset) % len(eligible)]
            if resource.in_flight == min_in_flight:
                return resource
        return eligible[start]

    async def has_available(self, *, skip: Optional[Set[str]] = None) -> bool:
        async with self._lock:
            return bool(self._eligible(skip))

    async def acquire(self, *, skip: Optional[Set[str]] = None) -> Optional[Resource]:
        async with self._lock:
            eligible = self._eligible(skip)
            if not eligible:
                return None
            resource = self._select_min_in_flight(eligible)
            self._cursor = (self._resources.index(resource) + 1) % len(self._resources)
            resource.in_flight += 1
            return resource

    async def release(self, resource: Resource) -> None:
        async with self._lock:
            resource.in_flight = max(0, resource.in_flight - 1)

    async def record_success(self, resource: Resource) -> None:
        async with self._lock:
            resource.total_requests += 1
            self._cooldown.reset(resource)

    async def record_failure(self, resource: Resource, error: ProviderError) -> None:
        async with self._lock:
            resource.total_requests += 1
            resource.total_failures += 1
            self._cooldown.apply_failure(resource)

    async def record_rate_limit(
        self, resource: Resource, retry_after: Optional[float] = None
    ) -> None:
        async with self._lock:
            resource.total_requests += 1
            resource.total_failures += 1
            self._cooldown.apply_rate_limit(resource, retry_after)
