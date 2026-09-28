"""Factory classes for the Gemini CLI Provider (TASK-008).

The Application layer never constructs GeminiCliProvider /
GeminiCliResource directly; it registers this package's ProviderDefinition
with the ProviderRegistry, which creates both on demand.
One OAuth credential (account) => one GeminiCliResource.
"""
from __future__ import annotations

from typing import Any, List, Optional

from providers.gemini_cli.provider import GeminiCliProvider
from providers.gemini_cli.resource import GeminiCliResource


class GeminiCliProviderFactory:
    """Creates a fresh GeminiCliProvider instance.

    The optional ``credential_store`` (AUTH-002) lets the provider resolve
    OAuth material from Credential objects referenced by
    ``resource.credential_id``; resources without a credential_id keep
    using their legacy fields.
    """

    def __init__(self, credential_store: Optional[Any] = None) -> None:
        self._credential_store = credential_store

    def create_provider(
        self,
        provider_id: str,
        config: Any = None,
    ) -> GeminiCliProvider:
        models = None
        proxy = None
        timeout_seconds = None
        if config and isinstance(config, dict):
            models = config.get("models")
            proxy_cfg = config.get("proxy")
            if proxy_cfg is not None:
                from transport.proxy import ProxyConfig

                if isinstance(proxy_cfg, ProxyConfig):
                    proxy = proxy_cfg
                else:
                    proxy = GeminiCliProvider.proxy_config_for(
                        GeminiCliResource(provider=provider_id, proxy=proxy_cfg)
                    )
            raw_timeout = config.get("timeout_seconds")
            if isinstance(raw_timeout, (int, float)):
                timeout_seconds = float(raw_timeout)
        kwargs: dict = {
            "models": models,
            "credential_store": self._credential_store,
            "proxy": proxy,
        }
        if timeout_seconds is not None:
            kwargs["timeout_seconds"] = timeout_seconds
        return GeminiCliProvider(**kwargs)


class GeminiCliResourceFactory:
    """Builds GeminiCliResource objects from config dicts."""

    def create_resources(
        self,
        provider_id: str,
        config: List[dict],
    ) -> List[GeminiCliResource]:
        resources = []
        for item in config:
            payload = dict(item)
            payload.setdefault("provider", provider_id)
            resources.append(GeminiCliResource.model_validate(payload))
        return resources
