"""Provider Registry: centralised provider lifecycle management.

Providers register with a factory callable. The registry creates and
caches instances on first access, making the lifecycle explicit and
keeping main.py free of provider-specific if/elif branches.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, TypeVar

from .provider import Provider

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Provider)

Factory = Callable[[], Provider]


class ProviderRegistry:
    """Thread-safe, lazily-instantiating provider registry."""

    def __init__(self) -> None:
        self._factories: Dict[str, Factory] = {}
        self._instances: Dict[str, Provider] = {}
        self._lock = asyncio.Lock()

    def register(self, provider_id: str, factory: Factory) -> None:
        """Register a provider factory.  Must be called before any get()."""
        if provider_id in self._factories:
            raise ValueError(f"provider '{provider_id}' already registered")
        self._factories[provider_id] = factory
        logger.info("provider.registered provider=%s", provider_id)

    def has(self, provider_id: str) -> bool:
        return provider_id in self._factories

    async def get(self, provider_id: str) -> Provider:
        """Return the cached instance, creating it on first access."""
        if provider_id in self._instances:
            return self._instances[provider_id]
        async with self._lock:
            if provider_id in self._instances:
                return self._instances[provider_id]
            factory = self._factories.get(provider_id)
            if factory is None:
                raise KeyError(f"provider '{provider_id}' not registered")
            instance = factory()
            self._instances[provider_id] = instance
            logger.info("provider.instantiated provider=%s", provider_id)
            return instance

    def list_ids(self) -> List[str]:
        """All registered provider ids (regardless of instantiation)."""
        return sorted(self._factories.keys())

    async def list_instances(self) -> List[Provider]:
        """All instantiated providers (for iteration)."""
        for pid in list(self._factories.keys()):
            await self.get(pid)
        return list(self._instances.values())

    def unregister(self, provider_id: str) -> None:
        """Remove a registration and its cached instance (if any)."""
        self._factories.pop(provider_id, None)
        self._instances.pop(provider_id, None)
        logger.info("provider.unregistered provider=%s", provider_id)
