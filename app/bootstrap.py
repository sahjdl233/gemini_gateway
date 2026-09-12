"""Application bootstrap: registers builtin providers with the registry.

TASK-001.5: main.py must NOT maintain a provider-specific factory map.
Provider registration lives here and is the single place to add a new
builtin provider.

TASK-002: anonymous_vertex (Anonymous Vertex / Agent Platform batchGraphql)
is now registered as the first real Google upstream.

TASK-004: firebase (Firebase AI Logic firebasevertexai) is registered as a
Gateway-native provider. One Firebase Project = one Resource.
"""

from __future__ import annotations

from core.provider_registry import ProviderDefinition, ProviderRegistry
from providers.anonymous_vertex.factory import (
    AnonymousVertexProviderFactory,
    AnonymousVertexResourceFactory,
)
from providers.fake.factory import FakeProviderFactory, FakeResourceFactory
from providers.firebase.factory import (
    FirebaseProviderFactory,
    FirebaseResourceFactory,
)
from providers.gemini_cli.factory import (
    GeminiCliProviderFactory,
    GeminiCliResourceFactory,
)


def register_builtin_providers(registry: ProviderRegistry) -> None:
    """Register every provider that ships with the application.

    fake (offline testing) and anonymous_vertex (real Google upstream).
    TASK-004/008: firebase and gemini_cli are registered as gateway-native.
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
    registry.register_definition(
        ProviderDefinition(
            provider_id="firebase",
            provider_factory=FirebaseProviderFactory(),
            resource_factory=FirebaseResourceFactory(),
        )
    )
    registry.register_definition(
        ProviderDefinition(
            provider_id="gemini_cli",
            provider_factory=GeminiCliProviderFactory(),
            resource_factory=GeminiCliResourceFactory(),
        )
    )

