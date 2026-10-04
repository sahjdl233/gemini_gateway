"""PostgreSQL persistence adapters (CONFIG/R-6 Part 2).

Standard-PostgreSQL implementations of the CONFIG/R-5 protocols:

* :class:`PostgresResourceDefinitionRepository` — async, over the
  ``resource_definitions`` table (reusing the table and row-mapping of
  the existing ``core.resource_postgres`` repository).
* :class:`PostgresCredentialRepository` — async facade over the
  existing AUTH-009 sync ``PostgreSQLCredentialRepository``: same table,
  same encryptor, no new crypto.  The sync calls are deliberate — the
  application's credential store is synchronous everywhere.
* :class:`PostgresRuntimeStateStore` — async, over the ``runtime_state``
  table.

The domain layer never sees any of this:
``RuntimeReconciliationService`` only knows the
``ResourceDefinitionRepository`` protocol.  Connections are duck-typed
(psycopg-3-style async ``execute``/``commit``/``rollback``/``close``),
so test doubles work without a driver import.
"""

from core.repositories.postgres.credential import (
    PostgresCredentialRepository,
)
from core.repositories.postgres.resource_definition import (
    PostgresResourceDefinitionRepository,
)
from core.repositories.postgres.runtime_state import (
    PostgresRuntimeStateStore,
)
from core.repositories.postgres.schema import initialize_persistence

__all__ = [
    "PostgresCredentialRepository",
    "PostgresResourceDefinitionRepository",
    "PostgresRuntimeStateStore",
    "initialize_persistence",
]
