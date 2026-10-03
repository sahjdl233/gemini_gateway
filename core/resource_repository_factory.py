"""ResourceRepository (durable sink) factory (DB-RESOURCE-011, Part A).

Selects the resource-store backend from config — the point where the
application "chooses" its store:

    resource_store:
      backend: memory      # default: no database required (Part B)
      # backend: postgres  # requires GEMINI_GATEWAY_DATABASE_URL

* ``memory`` (default, also the default when the section is absent) →
  :class:`core.resource_repository_memory.MemoryResourceRepository` —
  pre-011 behavior preserved; existing users never need a database.
* ``postgres`` → :class:`core.resource_postgres.PostgreSQLResourceRepository`
  over :func:`core.resource_postgres.psycopg_async_connection_factory`.
  The DSN comes from the shared ``GEMINI_GATEWAY_DATABASE_URL``
  environment variable (the credential store's precedent, AUTH-009);
  a missing DSN is a configuration error, never a silent memory
  fallback.
* Any other backend value is a fail-closed configuration error.

The factory constructs repositories only — it never connects (psycopg
connects lazily per operation), never runs bootstrap and never touches
the scheduler.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from core.resource_postgres import (
    PostgreSQLResourceRepository,
    psycopg_async_connection_factory,
)
from core.resource_repository import ResourceRepository
from core.resource_repository_memory import MemoryResourceRepository

__all__ = [
    "ResourceStoreConfigurationError",
    "resource_store_backend",
    "create_resource_repository",
]

#: Shared with the credential store (AUTH-009 precedent).
DATABASE_URL_ENV = "GEMINI_GATEWAY_DATABASE_URL"


class ResourceStoreConfigurationError(ValueError):
    """Invalid ``resource_store`` configuration (fail-closed)."""


def resource_store_backend(config: Mapping[str, Any]) -> str:
    """Read and validate ``resource_store.backend`` (default ``memory``)."""
    section = config.get("resource_store")
    if section is None:
        return "memory"
    if not isinstance(section, Mapping):
        raise ResourceStoreConfigurationError(
            "resource_store config section must be a mapping, got "
            f"{type(section).__name__}"
        )
    backend = section.get("backend", "memory")
    if not isinstance(backend, str) or backend not in ("memory", "postgres"):
        raise ResourceStoreConfigurationError(
            "resource_store.backend must be 'memory' or 'postgres', got "
            f"{backend!r}"
        )
    return backend


def create_resource_repository(config: Mapping[str, Any]) -> ResourceRepository:
    """Create the durable sink implied by ``resource_store.backend``."""
    backend = resource_store_backend(config)
    if backend == "memory":
        return MemoryResourceRepository()
    if backend == "postgres":
        dsn = os.environ.get(DATABASE_URL_ENV)
        if not dsn:
            raise ResourceStoreConfigurationError(
                f"resource_store.backend=postgres requires the "
                f"{DATABASE_URL_ENV} environment variable"
            )
        return PostgreSQLResourceRepository(psycopg_async_connection_factory(dsn))
    # resource_store_backend validates; this is unreachable defense.
    raise ResourceStoreConfigurationError(f"unknown backend: {backend!r}")
