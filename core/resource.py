"""The Resource is the central abstraction of the whole system.

Do NOT collapse everything into a single "Account".  Different Google
routes own different resource models (Vertex -> Egress/Session,
Firebase -> Project, CLI -> Credential/Account, ...). Providers extend
this base Resource with their own models.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel

from .health import HealthState


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Resource(BaseModel):
    id: str
    provider: str
    enabled: bool = True

    health: HealthState = HealthState.HEALTHY
    cooldown_until: Optional[datetime] = None

    in_flight: int = 0
    total_requests: int = 0
    total_failures: int = 0
    consecutive_failures: int = 0
