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
)

__all__ = [
    "create_resource_definition_repository",
    "resource_bootstrap_settings",
]


def create_resource_definition_repository(
    config: Mapping[str, Any],
) -> ResourceDefinitionRepository:
    """Create the definition repository implied by the config mapping.

    A non-empty ``providers`` section means the config is the definition
    seed: its entries are strictly parsed once into DTOs and served
    through a :class:`ConfigResourceDefinitionRepository`.  Without one
    there is nothing to parse — an empty
    :class:`MemoryResourceDefinitionRepository` is returned instead of
    inventing definitions.
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
