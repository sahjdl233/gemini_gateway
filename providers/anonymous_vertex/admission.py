"""Node admission domain model for Anonymous Vertex (ANON-008-A).

Pure domain layer between a NodeDefinition and the node pool:

    NodeDefinition -> Admission State -> Eligible Node

This module contains ONLY: the admission state enum, the admission result /
record value objects, the checker Protocol and the state-transition rules.
No HTTP, no Vertex calls, no reCAPTCHA, no DNS, no proxy probing, no pool
integration, no node deletion — those belong to ANON-008-B and later.

Design boundary: a NodeDefinition is identity/configuration and is never
mutated by admission; admission outcome is runtime state carried by
immutable value objects keyed by node_id.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

from providers.anonymous_vertex.node_definitions import NodeDefinition

__all__ = [
    "NodeAdmissionState",
    "NodeAdmissionResult",
    "NodeAdmissionRecord",
    "AdmissionChecker",
    "can_transition",
    "ILLEGAL_TRANSITIONS",
]


class NodeAdmissionState(str, Enum):
    """Lifecycle state of a node's most recent admission evaluation.

    UNKNOWN      newly imported, never checked
    TESTING      an admission check is running
    READY        passed the check; eligible for the candidate pool
    FAILED       most recent check failed (NOT a permanent deletion)
    QUARANTINED  explicitly isolated (repeated failures, manual disable,
                 risk-control rejection); recoverable only via a new check
    """

    UNKNOWN = "unknown"
    TESTING = "testing"
    READY = "ready"
    FAILED = "failed"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class NodeAdmissionResult:
    """Outcome of one admission check (immutable, JSON-serializable)."""

    state: NodeAdmissionState
    reason: Optional[str]
    checked_at: datetime

    def to_dict(self) -> dict:
        """JSON-safe payload: ``state`` as its string value, ``checked_at``
        as an ISO-8601 timestamp."""
        return {
            "state": self.state.value,
            "reason": self.reason,
            "checked_at": self.checked_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NodeAdmissionResult":
        return cls(
            state=NodeAdmissionState(data["state"]),
            reason=data.get("reason"),
            checked_at=datetime.fromisoformat(data["checked_at"]),
        )


@dataclass(frozen=True)
class NodeAdmissionRecord:
    """An admission result bound to one node identity.

    Records are append-only history entries for a node_id; they never
    touch the NodeDefinition (identity/config stays separate from runtime
    admission state).
    """

    node_id: str
    result: NodeAdmissionResult

    @property
    def state(self) -> NodeAdmissionState:
        return self.result.state

    @property
    def checked_at(self) -> datetime:
        return self.result.checked_at


@runtime_checkable
class AdmissionChecker(Protocol):
    """The only admission dependency: an async check of a NodeDefinition.

    Implementations (ANON-008-B) may probe endpoints, capabilities or
    credentials — this Protocol deliberately knows nothing about them, and
    the domain layer imports neither NodePool, ExecutionNode nor any
    transport implementation.
    """

    async def check(self, node: NodeDefinition) -> NodeAdmissionResult:
        """Run one admission check for ``node``."""
        ...


#: State changes must go through the check flow.  Anything not listed here
#: is forbidden — including UNKNOWN->READY, UNKNOWN->QUARANTINED and
#: QUARANTINED->READY (a quarantined node returns via TESTING only).
ALLOWED_TRANSITIONS = frozenset({
    (NodeAdmissionState.UNKNOWN, NodeAdmissionState.TESTING),
    (NodeAdmissionState.TESTING, NodeAdmissionState.READY),
    (NodeAdmissionState.TESTING, NodeAdmissionState.FAILED),
    (NodeAdmissionState.FAILED, NodeAdmissionState.TESTING),
    (NodeAdmissionState.FAILED, NodeAdmissionState.QUARANTINED),
    (NodeAdmissionState.READY, NodeAdmissionState.TESTING),
    (NodeAdmissionState.QUARANTINED, NodeAdmissionState.TESTING),
})


def can_transition(old: NodeAdmissionState, new: NodeAdmissionState) -> bool:
    """True when ``old -> new`` is a legal admission state transition."""
    return (old, new) in ALLOWED_TRANSITIONS


#: Explicitly documented illegal jumps (subset of the forbidden matrix,
#: called out because they bypass the check flow).
ILLEGAL_TRANSITIONS = frozenset({
    (NodeAdmissionState.UNKNOWN, NodeAdmissionState.READY),
    (NodeAdmissionState.UNKNOWN, NodeAdmissionState.QUARANTINED),
    (NodeAdmissionState.QUARANTINED, NodeAdmissionState.READY),
})
