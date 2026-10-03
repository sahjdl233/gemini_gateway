"""Resource definition repository factory (DB-RESOURCE-006).

The single composition point between application configuration and the
read-side :class:`core.resource_definition_repository.ResourceDefinitionRepository`
Protocol.  Application startup asks this module *what* the definition
source is; it never touches the DTO loader itself.

Responsibilities (deliberately narrow):

* create a :class:`ResourceDefinitionRepository` from an already-parsed
  config mapping — ``ConfigResourceDefinitionRepository`` when the config
  carries provider resource entries (the YAML-sourced seed), an empty
  ``MemoryResourceDefinitionRepository`` otherwise;
* parse the ``resource_bootstrap`` behavior section (``enabled`` /
  ``mode``) into a :class:`core.resource_bootstrap.BootstrapMode`.

Hard non-responsibilities: no runtime ``Resource`` construction, no
bootstrap execution, no database writes, no scheduler access, no
PostgreSQL definition repository yet.  ``source`` is not configurable —
the source of the definition repository *is* this factory's decision.
"""

from __future__ import annotations

from typing import Any, Mapping

from core.resource_bootstrap import BootstrapMode, ResourceBootstrapError
from core.resource_definition_loader import ConfigResourceDefinitionRepository
from core.resource_definition_repository import (
    MemoryResourceDefinitionRepository,
    ResourceDefinitionRepository,
    ResourceRepositoryDefinitionSource,
)
from core.resource_repository_factory import (
    create_resource_repository,
    resource_store_backend,
)

__all__ = [
    "create_resource_definition_repository",
    "create_config_definition_source",
    "resource_bootstrap_settings",
]


def create_resource_definition_repository(
    config: Mapping[str, Any],
) -> ResourceDefinitionRepository:
    """Create the definition source of record implied by the config.

    Dispatch (DB-RESOURCE-011, Part C):

    * ``resource_store.backend: postgres`` — the durable store IS the
      definition source of record: a
      :class:`ResourceRepositoryDefinitionSource` over the PostgreSQL
      repository (never a separate PG definition-repository class).
    * ``backend: memory`` (default) — a non-empty ``providers`` section
      makes the config the definition seed
      (:class:`ConfigResourceDefinitionRepository`); without one, an
      empty :class:`MemoryResourceDefinitionRepository` is returned
      instead of inventing definitions.

    NOTE: the bootstrap *incoming* seed is always the config, regardless
    of backend — use :func:`create_config_definition_source` for that
    role (YAML → database import semantics, ADR-002 §2).
    """
    if resource_store_backend(config) == "postgres":
        return ResourceRepositoryDefinitionSource(
            create_resource_repository(config)
        )
    return create_config_definition_source(config)


def create_config_definition_source(
    config: Mapping[str, Any],
) -> ResourceDefinitionRepository:
    """The config-backed definition source (the bootstrap incoming seed).

    Always parses the config's resource entries — independent of the
    backend — because importing the YAML seed into the durable store is
    bootstrap's job no matter where the store lives.
    """
    providers = config.get("providers")
    if isinstance(providers, Mapping) and providers:
        return ConfigResourceDefinitionRepository(config)
    return MemoryResourceDefinitionRepository()


_ALLOWED_MODES = (
    BootstrapMode.CHECK,
    BootstrapMode.IMPORT,
    BootstrapMode.OVERWRITE,
)


def resource_bootstrap_settings(
    config: Mapping[str, Any],
) -> tuple[bool, BootstrapMode]:
    """Read the ``resource_bootstrap`` behavior section.

    Returns ``(enabled, mode)``.  A missing/empty section disables
    bootstrap entirely (preserving pre-006 startup behavior); when
    enabled, ``mode`` must be one of ``check`` / ``import`` /
    ``overwrite`` — anything else is a startup-class configuration error
    (fail-closed, never a silent default).  ``source`` is deliberately
    not a config key: choosing the definition repository is this
    factory's job.
    """
    section = config.get("resource_bootstrap")
    if section is None:
        return False, BootstrapMode.CHECK
    if not isinstance(section, Mapping):
        raise ResourceBootstrapError(
            "resource_bootstrap config section must be a mapping, got "
            f"{type(section).__name__}"
        )
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ResourceBootstrapError(
            f"resource_bootstrap.enabled must be a boolean, got {enabled!r}"
        )
    raw_mode = section.get("mode", BootstrapMode.CHECK.value)
    try:
        mode = BootstrapMode(raw_mode)
    except ValueError:
        raise ResourceBootstrapError(
            f"resource_bootstrap.mode must be one of "
            f"{[m.value for m in _ALLOWED_MODES]}, got {raw_mode!r}"
        ) from None
    if mode not in _ALLOWED_MODES:
        raise ResourceBootstrapError(
            f"resource_bootstrap.mode must be one of "
            f"{[m.value for m in _ALLOWED_MODES]}, got {raw_mode!r}"
        )
    return enabled, mode
