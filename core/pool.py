"""Resource Pool contract.

Each provider owns its own pool of Resources (FirebasePool, VertexPool,
CLIPool, ...).  TASK-000 ships a generic in-memory pool.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Set

from .cooldown import CooldownManager
from .errors import ProviderError
from .health import HealthState
from .resource import Resource, ResourceKey


@dataclass(frozen=True)
class _RuntimeState:
    """The scheduling-relevant runtime state of a single Resource.

    TASK-STATE-001: only these fields survive a Resource object being
    re-created from a new definition.  Configuration / identity fields
    (id, provider, enabled, credential_id, provider-specific fields and any
    authentication material) are deliberately excluded: they always come
    from the *new* definition.  ``in_flight`` is excluded on purpose -- an
    in-flight count is never inherited, and a resource that still has active
    requests can never be reconciled at all (see
    :meth:`InMemoryPool.reconcile_resources`).
    """

    health: HealthState
    cooldown_until: Optional[datetime]
    consecutive_failures: int
    total_requests: int
    total_failures: int

    @classmethod
    def capture(cls, resource: Resource) -> "_RuntimeState":
        return cls(
            health=resource.health,
            cooldown_until=resource.cooldown_until,
            consecutive_failures=resource.consecutive_failures,
            total_requests=resource.total_requests,
            total_failures=resource.total_failures,
        )

    def apply_to(self, resource: Resource) -> None:
        resource.health = self.health
        resource.cooldown_until = self.cooldown_until
        resource.consecutive_failures = self.consecutive_failures
        resource.total_requests = self.total_requests
        resource.total_failures = self.total_failures


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

    async def reconcile_resources(self, new_resources: List[Resource]) -> None:
        """Replace resources while preserving per-ResourceKey runtime state.

        TASK-STATE-001:
        - Runtime state (health, cooldown_until, consecutive_failures,
          total_requests, total_failures) is preserved by ResourceKey.
        - Configuration fields (id, provider, enabled, credential_id, etc)
          always come from the new Resource object.
        - in_flight is never preserved and must be zero on any replaced
          resource; otherwise the operation fails.
        - The cursor tie-breaker is advanced as little as reasonably possible.

        The method is atomic: on any validation error the pool state is left
        untouched.
        """
        async with self._lock:
            # Fast path: if the new list equals the old list (object identity)
            # we can skip all work.
            if self._resources is new_resources:
                return

            # 1) Build a map of old runtime state keyed by ResourceKey.
            # We capture the state into _RuntimeState for atomicity.
            old_by_key: dict[ResourceKey, _RuntimeState] = {}
            for r in self._resources:
                # The current resource's state
                state = _RuntimeState.capture(r)
                # Store a copy that includes the current in_flight for validation
                # (Note: _RuntimeState as defined doesn't store in_flight, 
                # but we need it for the check).
                # Let's just use the resource objects for in_flight checks.
                old_by_key[r.resource_key] = state

            # 2) Validate in_flight constraints and build the new list.
            new_list: list[Resource] = []
            for nr in new_resources:
                key = nr.resource_key
                
                # Check in_flight for the old resource with the same key
                old_res = next((r for r in self._resources if r.resource_key == key), None)
                if old_res and old_res.in_flight > 0:
                    raise RuntimeError(
                        f"Cannot reconcile resource {nr.provider}:{nr.id} "
                        f"because it has {old_res.in_flight} in-flight requests"
                    )

                # Deep-copy the new resource definition so we never mutate the
                # caller's object.
                nr_copy = Resource.model_validate(nr.model_dump())

                # Apply runtime state if it existed
                if key in old_by_key:
                    old_by_key[key].apply_to(nr_copy)
                
                new_list.append(nr_copy)

            # Check for removed resources that are still in-flight
            new_keys = {r.resource_key for r in new_resources}
            for r in self._resources:
                if r.resource_key not in new_keys and r.in_flight > 0:
                    raise RuntimeError(
                        f"Cannot reconcile pool because resource {r.provider}:{r.id} "
                        f"is being removed while having {r.in_flight} in-flight requests"
                    )

            # 3) Preserve cursor tie-breaker as far as possible.
            old_cursor = self._cursor
            old_len = len(self._resources)

            if old_len == 0:
                new_cursor = 0
            else:
                cursor_key = self._resources[old_cursor % old_len].resource_key
                try:
                    new_index = next(
                        i for i, r in enumerate(new_list) if r.resource_key == cursor_key
                    )
                except StopIteration:
                    # cursor target removed: scan forward from old position for
                    # the first surviving ResourceKey
                    new_index = 0
                    for offset in range(old_len):
                        probe = (old_cursor + offset) % old_len
                        probe_key = self._resources[probe].resource_key
                        if probe_key in new_keys:
                            new_index = next(
                                i for i, r in enumerate(new_list) if r.resource_key == probe_key
                            )
                            break

                new_cursor = new_index

            # 4) Atomic commit
            self._resources = new_list
            self._cursor = new_cursor
