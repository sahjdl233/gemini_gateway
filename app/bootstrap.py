"""Application bootstrap: registers builtin providers with the registry.

TASK-001.5: main.py must NOT maintain a provider-specific factory map.
Provider registration lives here and is the single place to add a new
builtin provider (fake today; firebase/vertex/cli/build later).
"""

from __future__ import annotations

from core.provider_registry import ProviderDefinition, ProviderRegistry
from providers.fake.factory import FakeProviderFactory, FakeResourceFactory


def register_builtin_providers(registry: ProviderRegistry) -> None:
    """Register every provider that ships with the application.

    Only the fake provider is registered in this phase.  Real Google
    providers (Firebase/Vertex/CLI/Build) are intentionally NOT registered
    here until their corresponding tasks.
    """
    registry.register_definition(
        ProviderDefinition(
            provider_id="fake",
            provider_factory=FakeProviderFactory(),
            resource_factory=FakeResourceFactory(),
        )
    )
