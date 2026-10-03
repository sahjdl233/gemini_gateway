"""Config → ResourceDefinition adapter (DB-RESOURCE-003-2).

Converts the already-parsed gateway config mapping — the output of the
existing config loader (``load_config`` / ``default_config``), never a
YAML file — into strict :class:`core.resource_definition.ResourceDefinitionBase`
DTOs, ready to hand to the DB-RESOURCE-003-1
:class:`core.resource_bootstrap.ResourceBootstrapService`.

Boundary rules (DESIGN-002 §7; TASK-DB-RESOURCE-003-2):

* No YAML, no file I/O, no ``Path``, nothing from ``app/``.  File reading
  stays with the existing config loader.
* Provider identity comes from the outer ``providers.<provider_id>`` key.
  An embedded ``provider`` inside a resource entry is accepted only when
  it matches the outer id — a mismatch fails, it is never silently
  overwritten.
* Provider discrimination is delegated to the single source of truth,
  :func:`core.resource_definition.parse_resource_definition`.  This
  module implements no per-provider field logic and no fallback type.
* Strict validation only: unknown providers, secret fields, runtime
  fields, cross-provider fields and malformed provider-specific fields
  all fail with full context (provider, resource entry index,
  resource_id) and the original validation error chained as
  ``__cause__``.  Legacy secret fields are never stripped to make an
  entry pass — that would turn illegal input into a "valid" DTO
  (AUTH-010 owns credential migration, not this module).
* ``providers.<provider>.enabled`` is runtime/provider-layer config, not
  a ResourceDefinition field: resources are loaded regardless of it, and
  it never enters a DTO.  Each resource's own ``enabled`` passes through
  to the DTO unchanged (the DTO owns its default).
* Duplicate ``(provider, resource_id)`` identities are NOT rejected here
  — duplicate identity is bootstrap plan validation (003-1).  The loader
  may return two legal DTOs with the same identity.
* Output is deterministic: ascending by ``(provider, resource_id)``,
  independent of config ordering — so bootstrap plans, CLI reports and
  test results are stable.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional

from core.resource_definition import (
    ResourceDefinitionBase,
    parse_resource_definition,
)
from core.resource_definition_repository import (
    MemoryResourceDefinitionRepository,
)

__all__ = [
    "ResourceDefinitionLoadError",
    "load_resource_definitions",
    "ConfigResourceDefinitionRepository",
]


class ResourceDefinitionLoadError(Exception):
    """A resource entry in the config mapping failed strict validation.

    Carries the locator context needed to point at the offending entry:

    * ``provider`` — the outer provider id the entry belongs to;
    * ``resource_id`` — the entry's ``id`` when extractable, else None;
    * ``index`` — the entry's position within that provider's
      ``resources`` list.

    ``__cause__`` preserves the original :class:`pydantic.ValidationError`
    or :class:`core.resource_definition.ResourceDefinitionError` — never
    flattened into a generic "invalid resource" message.
    """

    def __init__(
        self,
        *,
        provider: str,
        index: int,
        resource_id: Optional[str],
        message: str,
    ) -> None:
        self.provider = provider
        self.resource_id = resource_id
        self.index = index
        super().__init__(message)


def _entry_context(
    provider: str,
    index: int,
    entry: Any,
    problem: str,
) -> ResourceDefinitionLoadError:
    resource_id = entry.get("id") if isinstance(entry, Mapping) else None
    return ResourceDefinitionLoadError(
        provider=provider,
        index=index,
        resource_id=resource_id if isinstance(resource_id, str) else None,
        message=(
            f"invalid resource definition: provider={provider!r}, "
            f"resource_id={resource_id!r}, index={index}: {problem}"
        ),
    )


def load_resource_definitions(
    config: Mapping[str, Any],
) -> List[ResourceDefinitionBase]:
    """Extract and strictly validate all resource definitions from a
    parsed config mapping.

    Reads ``config["providers"][<provider_id>]["resources"]``; missing
    ``providers``, missing/empty ``resources`` all yield ``[]`` — no
    definitions are invented.  Provider-level ``enabled`` is ignored by
    design (runtime-layer config; see module docstring).
    """
    providers = config.get("providers")
    if providers is None:
        return []
    if not isinstance(providers, Mapping):
        raise ResourceDefinitionLoadError(
            provider="<config>",
            index=0,
            resource_id=None,
            message=(
                "config 'providers' must be a mapping, got "
                f"{type(providers).__name__}"
            ),
        )

    loaded: List[ResourceDefinitionBase] = []
    for provider_id, provider_cfg in providers.items():
        if not isinstance(provider_id, str) or not provider_id:
            raise ResourceDefinitionLoadError(
                provider=repr(provider_id),
                index=0,
                resource_id=None,
                message=f"provider id must be a non-empty string, got {provider_id!r}",
            )
        if provider_cfg is None:
            continue
        if not isinstance(provider_cfg, Mapping):
            raise ResourceDefinitionLoadError(
                provider=provider_id,
                index=0,
                resource_id=None,
                message=(
                    f"provider config for {provider_id!r} must be a "
                    f"mapping, got {type(provider_cfg).__name__}"
                ),
            )
        resources = provider_cfg.get("resources")
        if resources is None:
            continue
        if not isinstance(resources, list):
            raise ResourceDefinitionLoadError(
                provider=provider_id,
                index=0,
                resource_id=None,
                message=(
                    f"provider {provider_id!r} 'resources' must be a list, "
                    f"got {type(resources).__name__}"
                ),
            )

        for index, entry in enumerate(resources):
            if not isinstance(entry, Mapping):
                raise _entry_context(
                    provider_id, index, entry,
                    f"resource entry must be a mapping, got {type(entry).__name__}",
                )
            payload = dict(entry)

            embedded = payload.get("provider")
            if embedded is not None and embedded != provider_id:
                raise _entry_context(
                    provider_id, index, entry,
                    f"embedded provider {embedded!r} does not match the "
                    f"owning provider section {provider_id!r}",
                )
            # Inject the outer provider discriminator; entries are not
            # required to repeat it.
            payload["provider"] = provider_id

            try:
                loaded.append(parse_resource_definition(payload))
            except Exception as exc:
                # UnknownProviderError, CredentialBearingProxyError and
                # pydantic ValidationError all land here with their
                # original error chained as __cause__ — never flattened.
                raise _entry_context(
                    provider_id, index, entry,
                    f"failed strict DTO validation: {exc}",
                ) from exc

    loaded.sort(key=lambda d: (d.provider, d.id))
    return loaded


class ConfigResourceDefinitionRepository(MemoryResourceDefinitionRepository):
    """The config adapter as a :class:`ResourceDefinitionRepository`.

    Parses the config mapping once (via :func:`load_resource_definitions`)
    and serves the resulting DTOs through the source-agnostic read
    interface.  Consumers of the repository see definitions identical to
    the DB-backed implementation's — where they come from is invisible.
    """

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(load_resource_definitions(config))
