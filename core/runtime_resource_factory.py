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

from typing import Any, Callable, Dict, Iterable, List

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


def registry_runtime_builder(registry: Any) -> Callable[
    [Iterable[ResourceDefinitionBase]], Dict[str, List[Any]]
]:
    """Return a ``runtime_builder`` for
    :class:`core.runtime_reconciliation.RuntimeReconciliationService`.

    The builder groups definitions by their provider discriminant,
    converts each group via :func:`runtime_payloads_for_provider` and
    creates the provider's runtime resources.  The registry only ever
    sees dict payloads.
    """

    def build(
        definitions: Iterable[ResourceDefinitionBase],
    ) -> Dict[str, List[Any]]:
        grouped: Dict[str, List[ResourceDefinitionBase]] = {}
        for definition in definitions:
            grouped.setdefault(definition.provider, []).append(definition)
        built: Dict[str, List[Any]] = {}
        for provider_id in sorted(grouped):
            built[provider_id] = create_runtime_resources(
                registry,
                grouped[provider_id],
                provider_id=provider_id,
            )
        return built

    return build


def create_config_resource_source(registry: Any) -> Callable[..., List[Any]]:
    """Return the resource source for the legacy YAML path.

    The returned callable maps ``(provider_id, raw config entries)`` to
    runtime resources through the registry — the fallback source used
    when bootstrap is disabled.  Kept beside the DTO conversion so every
    Resource-creation call site lives in this module.
    """

    def source(provider_id: str, entries: List[Dict[str, Any]]) -> List[Any]:
        return registry.create_resources(provider_id, entries)

    return source
