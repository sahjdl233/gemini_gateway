"""Factories for the FakeProvider package (TASK-001.5).

The Application layer never constructs FakeProvider or FakeResource
directly.  Instead it registers this package's ProviderDefinition with
the ProviderRegistry, which creates both provider and resources on
demand.
"""

from __future__ import annotations

from typing import List

from core.resource import Resource
from core.resource_factory import ResourceFactory
from providers.fake.provider import FakeProvider, FakeResource


class FakeProviderFactory:
    """Creates a fresh FakeProvider instance."""

    def create_provider(self, provider_id: str) -> FakeProvider:
        return FakeProvider()


class FakeResourceFactory:
    """Builds FakeResource objects from config dicts."""

    def create_resources(
        self, provider_id: str, config: List[dict]
    ) -> List[FakeResource]:
        resources = []
        for item in config:
            payload = dict(item)
            payload.setdefault("provider", provider_id)
            payload.setdefault("scenario", "success")
            resources.append(FakeResource.model_validate(payload))
        return resources
