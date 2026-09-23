from __future__ import annotations

from typing import Any, Dict, List

from execution.http import (
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_MAX_KEEPALIVE_CONNECTIONS,
)
from transport.proxy import ProxyConfig

from .provider import AntigravityProvider
from .resource import AntigravityResource


class AntigravityProviderFactory:
    """Creates AntigravityProvider instances.

    Provider level == backend level: one provider owns one
    HttpExecutionBackend, which owns one persistent AsyncClient shared by
    every AntigravityResource. Resource level stays lightweight scheduling
    metadata and never owns transport (TASK-ARCH-003).
    """

    def create_provider(self, provider_id: str, config: Any = None) -> AntigravityProvider:
        # Provider must not bind to any account/resource: the Scheduler
        # injects the selected AntigravityResource into complete()/stream().
        return AntigravityProvider(**_backend_kwargs(config))


def _backend_kwargs(config: Any) -> Dict[str, Any]:
    """Extract backend transport options; credentials never live here."""
    kwargs: Dict[str, Any] = {}
    if not isinstance(config, dict):
        return kwargs

    timeout = config.get("timeout_seconds")
    if isinstance(timeout, (int, float)) and timeout > 0:
        kwargs["timeout_seconds"] = float(timeout)

    proxy = config.get("proxy")
    if isinstance(proxy, dict):
        kwargs["proxy"] = ProxyConfig(
            scheme=proxy.get("scheme", "direct"),
            host=proxy.get("host"),
            port=proxy.get("port"),
            username=proxy.get("username"),
            password=proxy.get("password"),
        )

    for key in ("max_connections", "max_keepalive_connections"):
        value = config.get(key)
        if isinstance(value, int) and value > 0:
            kwargs[key] = value

    return kwargs


class AntigravityResourceFactory:
    def create_resources(self, provider_id: str, config: list) -> list:
        resources = []
        for item in config:
            payload = dict(item)
            payload.setdefault('provider', provider_id)
            payload.setdefault('ide_type', 'ANTIGRAVITY')
            resources.append(AntigravityResource(**payload))
        return resources
