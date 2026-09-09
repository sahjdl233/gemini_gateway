"""Factory classes for Anonymous Vertex Provider (TASK-002).

The Application layer never constructs AnonymousVertexProvider or
AnonymousVertexResource directly. It registers this packages ProviderDefinition
with the ProviderRegistry, which creates both provider and resources on demand.
"""
from __future__ import annotations

from typing import Any, List

from core.provider_registry import ProviderFactory
from core.resource import Resource
from core.resource_factory import ResourceFactory
from providers.anonymous_vertex.provider import AnonymousVertexProvider
from providers.anonymous_vertex.resource import AnonymousVertexResource


class AnonymousVertexProviderFactory:
    """Creates a fresh AnonymousVertexProvider instance."""

    def create_provider(self, provider_id: str) -> AnonymousVertexProvider:
        return AnonymousVertexProvider()


class AnonymousVertexResourceFactory:
    """Builds AnonymousVertexResource objects from config dicts."""

    def create_resources(
        self, provider_id: str, config: List[dict]
    ) -> List[AnonymousVertexResource]:
        resources = []
        for item in config:
            payload = dict(item)
            payload.setdefault("provider", provider_id)
            resources.append(AnonymousVertexResource.model_validate(payload))
        return resources
