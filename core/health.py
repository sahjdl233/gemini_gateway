"""Health state model for Resources."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class HealthState(str, enum.Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    COOLDOWN = "COOLDOWN"
    DISABLED = "DISABLED"


@dataclass
class HealthResult:
    """Result of a Provider.health_check(resource) call."""

    state: HealthState
    message: str = ""
    consecutive_failures: int = 0
    last_checked: Optional[str] = None
    extra: dict = field(default_factory=dict)
