"""Runtime resource build from ResourceDefinition DTOs (DB-RESOURCE-007).

The conversion layer between the persisted definition world and the
runtime world:

    ResourceDefinitionBase
        ->  to_runtime_definition()      (pure dict payload)
        ->  ProviderRegistry.create_resources(provider_id, payloads)
        ->  Resource

Deliberately narrow: no bootstrap, no scheduler, no pool, no YAML, no
app wiring.  The registry keeps speaking plain dict payloads (Part C) —
converting DTOs into those payloads is entirely this module's job, and
runtime state is never written back onto definitions.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from core.resource_definition import ResourceDefinitionBase

__all__ = [
    "runtime_payloads_for_provider",
    "create_runtime_resources",
]


def runtime_payloads_for_provider(
    definitions: Iterable[ResourceDefinitionBase],
    provider_id: str,
) -> List[Dict[str, Any]]:
    """Project definitions of one provider into runtime payloads.

    Each payload is exactly :meth:`ResourceDefinitionBase.to_runtime_definition`
    — the provider-specific allowlisted body plus the common identity
    fields.  Definitions belonging to other providers are skipped, so a
    caller can hand this the whole sink listing.
    """
    return [
        definition.to_runtime_definition()
        for definition in definitions
        if definition.provider == provider_id
    ]


def create_runtime_resources(
    registry: Any,
    definitions: Iterable[ResourceDefinitionBase],
    *,
    provider_id: str,
) -> List[Any]:
    """Build runtime ``Resource`` objects for one provider from DTOs.

    ``registry`` is a :class:`core.provider_registry.ProviderRegistry`
    (duck-typed: only ``create_resources`` is used, and it only ever sees
    dict payloads).  Raises whatever the registry raises for unknown
    providers or invalid payloads — conversion adds no error mapping.
    """
    payloads = runtime_payloads_for_provider(definitions, provider_id)
    return registry.create_resources(provider_id, payloads)
