"""Persistence layer foundation (CONFIG/R-5).

Protocol-first abstractions for the three persistence concerns, so the
Postgres adapter (next task) has one place to land:

    ResourceDefinitionRepository   definitions (reference + config only)
    CredentialRepository           secret material (get/save by id)
    RuntimeStateStore              discardable scheduling state

Boundaries frozen by docs/ADR-CONFIG-R4-PERSISTENCE-BOUNDARY.md:

* definition repositories NEVER carry runtime state or resolve
  credentials;
* credential repositories expose ONLY material in / material out —
  callers never touch storage;
* runtime state is optional and discardable: a missing store, or a
  missing entry, must never affect startup.

Deliberately additive: the existing ABCs (core.resource_repository,
core.credential.CredentialRepository) and read Protocol
(core.resource_definition_repository) remain in place until the adapter
migration lands.  No SQLAlchemy, no migrations, no Admin API wiring.
"""

from core.repositories.credential import CredentialMaterial, CredentialRepository
from core.repositories.resource_definition import ResourceDefinitionRepository
from core.repositories.runtime_state import RuntimeState, RuntimeStateStore

__all__ = [
    "CredentialMaterial",
    "CredentialRepository",
    "ResourceDefinitionRepository",
    "RuntimeState",
    "RuntimeStateStore",
]
