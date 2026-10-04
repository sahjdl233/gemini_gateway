"""In-memory persistence implementations (CONFIG/R-5 Part D).

Real, runnable objects for tests and runtime wiring — the memory tier of
the persistence layer.  No database, no encryption: these are the
reference semantics the future Postgres adapter must match
(``tests/core/test_repository_contract.py`` runs the same suite against
every implementation).
"""

from core.repositories.memory.credential import MemoryCredentialRepository
from core.repositories.memory.resource_definition import (
    MemoryResourceDefinitionRepository,
)
from core.repositories.memory.runtime_state import MemoryRuntimeStateStore

__all__ = [
    "MemoryCredentialRepository",
    "MemoryResourceDefinitionRepository",
    "MemoryRuntimeStateStore",
]
