"""Factory classes for the Firebase Provider (TASK-004).

The Application layer never constructs FirebaseProvider or
FirebaseResource directly. It registers this package's ProviderDefinition
with the ProviderRegistry, which creates both provider and resources on
demand. One Firebase Project = one FirebaseResource.
"""
from __future__ import annotations

from typing import Any, List, Optional

from providers.firebase.provider import FirebaseProvider
from providers.firebase.resource import FirebaseResource


class FirebaseProviderFactory:
    """Creates a fresh FirebaseProvider instance.

    The optional config may carry an explicit model list ("models") used
    to override the default snapshot. If absent, the provider falls back
    to DEFAULT_MODELS (config-driven; TASK-003: no firebase2api table copy).

    The optional ``credential_store`` (AUTH-002) lets the provider resolve
    project credentials from Credential objects referenced by
    ``resource.credential_id``; resources without a credential_id keep
    using their legacy fields.
    """

    def __init__(self, credential_store: Optional[Any] = None) -> None:
        self._credential_store = credential_store

    def create_provider(
        self, provider_id: str, config: Any = None
    ) -> FirebaseProvider:
        models = None
        if config and isinstance(config, dict):
            models = config.get("models")
        return FirebaseProvider(
            models=models,
            credential_store=self._credential_store,
        )


class FirebaseResourceFactory:
    """Builds FirebaseResource objects from config dicts."""

    def create_resources(
        self, provider_id: str, config: List[dict]
    ) -> List[FirebaseResource]:
        resources = []
        for item in config:
            payload = dict(item)
            payload.setdefault("provider", provider_id)
            resources.append(FirebaseResource.model_validate(payload))
        return resources

