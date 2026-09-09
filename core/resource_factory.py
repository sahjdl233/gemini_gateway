"""Resource Factory contract (TASK-001.5).

Decouples provider-specific Resource creation from the Application layer.
Each provider registers its own ResourceFactory with the ProviderRegistry.
The Application layer only asks the registry to create resources by
provider id -- it never learns the concrete Resource type.
"""

from __future__ import annotations

from typing import List, Protocol

from .resource import Resource


class ResourceFactory(Protocol):
    """Creates provider-owned Resources from raw config dicts."""

    def create_resources(
        self,
        provider_id: str,
        config: List[dict],
    ) -> List[Resource]:
        """Build the provider's resources for the given provider_id.

        config is the resources section of a provider block from the
        application configuration (a list of dicts).  The concrete Resource
        subclass is chosen by the factory, never by the Application layer.
        """
        ...
