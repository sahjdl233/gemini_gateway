"""Provider Registry: centralised provider lifecycle management.

Providers register a ProviderDefinition (a provider factory plus a
resource factory).  The registry creates and caches provider instances
and creates provider-owned resources, keeping main.py free of any
provider-specific if/elif branches and concrete Resource types.

TASK-001.5: the registry now owns BOTH provider creation and resource
creation so the Application layer never imports a concrete Provider or
Resource.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol, TypeVar

from .provider import Provider
from .resource import Resource
from .resource_factory import ResourceFactory

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Provider)

Factory = Callable[[], Provider]


class ProviderFactory(Protocol):
    """Creates a single provider instance from its provider id."""

    def create_provider(self, provider_id: str) -> Provider:
        ...


@dataclass(frozen=True)
class ProviderDefinition:
    """A registered provider: how to build the provider and its resources.

    Keeping both factories together means a provider's Resource type is
    decided by the provider package, never by the Application layer.
    """

    provider_id: str
    provider_factory: ProviderFactory
    resource_factory: ResourceFactory


class UnknownProviderError(ValueError):
    """Raised when a provider id is not registered (no fallback to fake)."""


class ProviderRegistry:
    """Thread-safe, lazily-instantiating provider registry."""

    def __init__(self) -> None:
        self._definitions: Dict[str, ProviderDefinition] = {}
        self._instances: Dict[str, Provider] = {}
        self._lock = asyncio.Lock()

    def register_definition(self, definition: ProviderDefinition) -> None:
        """Register a full provider definition (provider + resource factory)."""
        if definition.provider_id in self._definitions:
            raise ValueError(
                "provider '" + definition.provider_id + "' already registered"
            )
        self._definitions[definition.provider_id] = definition
        logger.info("provider.registered provider=%s", definition.provider_id)

    def register(self, provider_id: str, factory: Factory) -> None:
        """Register a provider factory (backwards-compatible helper).

        When only a provider factory is supplied there is no resource
        factory; resource creation via create_resources will raise.
        """
        if provider_id in self._definitions:
            raise ValueError("provider '" + provider_id + "' already registered")

        class _FactoryAdapter:
            def __init__(self, f: Factory) -> None:
                self._f = f

            def create_provider(self, provider_id: str) -> Provider:
                return self._f()

        class _NoResourceFactory:
            def create_resources(self, provider_id: str, config: List[dict]) -> List[Resource]:
                raise NotImplementedError(
                    "provider '" + provider_id + "' has no resource factory registered"
                )

        self._definitions[provider_id] = ProviderDefinition(
            provider_id=provider_id,
            provider_factory=_FactoryAdapter(factory),
            resource_factory=_NoResourceFactory(),
        )
        logger.info("provider.registered provider=%s", provider_id)

    def has(self, provider_id: str) -> bool:
        return provider_id in self._definitions

    def definition(self, provider_id: str) -> ProviderDefinition:
        if provider_id not in self._definitions:
            raise UnknownProviderError(
                "provider '" + provider_id + "' is not registered"
            )
        return self._definitions[provider_id]

    def _require(self, provider_id: str) -> ProviderDefinition:
        if provider_id not in self._definitions:
            raise UnknownProviderError(
                "provider '" + provider_id + "' is not registered"
            )
        return self._definitions[provider_id]

    def create(self, provider_id: str, config: Any = None) -> Provider:
        """Create a NEW provider instance for the given provider id.

        Unlike get (which caches), create returns a fresh instance each
        call.  Used at application build time to wire providers into the
        Scheduler.  Raises UnknownProviderError for unknown ids.
        """
        definition = self._require(provider_id)
        provider = definition.provider_factory.create_provider(provider_id)
        logger.info("provider.created provider=%s", provider_id)
        return provider

    def create_resources(
        self, provider_id: str, config: List[dict]
    ) -> List[Resource]:
        """Create the provider's resources from config dicts.

        Raises UnknownProviderError for unknown ids.
        """
        definition = self._require(provider_id)
        resources = definition.resource_factory.create_resources(
            provider_id, config
        )
        logger.info("resource.created provider=%s count=%d", provider_id, len(resources))
        return resources

    async def get(self, provider_id: str) -> Provider:
        """Return the cached instance, creating it on first access."""
        if provider_id in self._instances:
            return self._instances[provider_id]
        async with self._lock:
            if provider_id in self._instances:
                return self._instances[provider_id]
            definition = self._definitions.get(provider_id)
            if definition is None:
                raise KeyError("provider '" + provider_id + "' not registered")
            instance = definition.provider_factory.create_provider(provider_id)
            self._instances[provider_id] = instance
            logger.info("provider.instantiated provider=%s", provider_id)
            return instance

    def list_ids(self) -> List[str]:
        """All registered provider ids (regardless of instantiation)."""
        return sorted(self._definitions.keys())

    async def list_instances(self) -> List[Provider]:
        """All instantiated providers (for iteration)."""
        for pid in list(self._definitions.keys()):
            await self.get(pid)
        return list(self._instances.values())

    def unregister(self, provider_id: str) -> None:
        """Remove a registration and its cached instance (if any)."""
        self._definitions.pop(provider_id, None)
        self._instances.pop(provider_id, None)
        logger.info("provider.unregistered provider=%s", provider_id)
