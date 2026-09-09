"""The Resource is the central abstraction of the whole system.

Do NOT collapse everything into a single "Account".  Different Google
routes own different resource models (Vertex -> Egress/Session,
Firebase -> Project, CLI -> Credential/Account, ...). Providers extend
this base Resource with their own models.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, computed_field

from .health import HealthState


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ResourceKey:
    """Immutable compound identity: provider_id + resource_id.

    Ensures 'firebase:project-01' and 'vertex:project-01' are always
    distinct, even when both resources share the same local id.
    """

    __slots__ = ("_provider", "_id", "_hash")

    def __init__(self, provider: str, id: str) -> None:
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_id", id)
        object.__setattr__(self, "_hash", hash((provider, id)))

    @property
    def provider(self) -> str:
        return self._provider  # type: ignore[return-value]

    @property
    def id(self) -> str:
        return self._id  # type: ignore[return-value]

    def __str__(self) -> str:
        return f"{self._provider}:{self._id}"

    def __repr__(self) -> str:
        return f"ResourceKey(provider={self._provider!r}, id={self._id!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ResourceKey):
            return NotImplemented
        return self._provider == other._provider and self._id == other._id

    def __hash__(self) -> int:
        return self._hash


class Resource(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    id: str
    provider: str
    enabled: bool = True

    health: HealthState = HealthState.HEALTHY
    cooldown_until: Optional[datetime] = None

    in_flight: int = 0
    total_requests: int = 0
    total_failures: int = 0
    consecutive_failures: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resource_key(self) -> ResourceKey:
        return ResourceKey(provider=self.provider, id=self.id)
