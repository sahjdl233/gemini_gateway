"""Application bootstrap: registers builtin providers with the registry.

TASK-001.5: main.py must NOT maintain a provider-specific factory map.
Provider registration lives here and is the single place to add a new
builtin provider.

TASK-002: anonymous_vertex (Anonymous Vertex / Agent Platform batchGraphql)
is now registered as the first real Google upstream.
"""

from __future__ import annotations

from core.provider_registry import ProviderDefinition, ProviderRegistry
from providers.anonymous_vertex.factory import (
    AnonymousVertexProviderFactory,
    AnonymousVertexResourceFactory,
)
from providers.fake.factory import FakeProviderFactory, FakeResourceFactory


def register_builtin_providers(registry: ProviderRegistry) -> None:
    """Register every provider that ships with the application.

    fake (offline testing) and anonymous_vertex (real Google upstream).
    Other Google providers (Firebase/Vertex/CLI/Build) are NOT registered
    until their corresponding tasks.
    """
    registry.register_definition(
        ProviderDefinition(
            provider_id="fake",
            provider_factory=FakeProviderFactory(),
            resource_factory=FakeResourceFactory(),
        )
    )
    registry.register_definition(
        ProviderDefinition(
            provider_id="anonymous_vertex",
            provider_factory=AnonymousVertexProviderFactory(),
            resource_factory=AnonymousVertexResourceFactory(),
        )
    )

