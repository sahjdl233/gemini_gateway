"""Runtime snapshot DTO (DB-RESOURCE-008, Part B).

An immutable-ish record of one runtime reconciliation pass.  It exists
so that ``app.state`` never becomes the only holder of runtime resource
state: future reload, admin API and health-check features can consume a
snapshot without reaching into the scheduler.

``resources`` is the flat, deterministic view (ascending by provider
then pool order); ``resources_by_provider`` is the pool-shaped view the
scheduler build consumes.  Only ``Resource`` instances live here —
never definitions, never scheduler state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RuntimeSnapshot:
    """Result of one runtime reconciliation pass."""

    #: Flat list of runtime Resource instances (deterministic order).
    resources: List[Any] = field(default_factory=list)
    #: When the snapshot was generated (UTC; injectable clock for tests).
    generated_at: datetime = field(default_factory=utcnow)
    #: How many definitions the source repository served.
    source_count: int = 0
    #: Pool-shaped view: provider id -> Resource instances for its pool.
    resources_by_provider: Dict[str, List[Any]] = field(default_factory=dict)

    def resource_count(self) -> int:
        return len(self.resources)
