"""Node definition / runtime-state boundary for Anonymous Vertex (ANON-006).

Three distinct concepts with disjoint lifecycles:

    NodeDefinition   static, persistable, identity+configuration only
                     (frozen: runtime code must never mutate it)
    NodeSource       where a definition came from (subscription | txt | manual)
    NodeRuntimeState discardable counters / scheduling state, keyed by node_id

    NodeDefinition  !=  NodeRuntimeState  !=  NodeLease

Serialization guarantees (enforced by tests):

* ``NodeDefinition.to_dict()`` carries NO runtime field (no in-flight
  counters, no cooldown, no latency, no last_error, no admission result);
* ``NodeRuntimeState.to_dict()`` carries NO definition-owned field
  (no proxy, weight, max_concurrency, enabled flag or source payload).
  ``node_id`` appears in both as the join key for a future persistence
  merge policy (out of scope here).

No import / TXT parsing / probing / storage is implemented in this module.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from types import MappingProxyType
from typing import Any, Dict, Optional


def _freeze(value: Any) -> Any:
    """Recursively convert JSON-ish structures into immutable ones.

    dict -> MappingProxyType, list -> tuple, set -> frozenset (nested
    included), so a caller holding an internal mapping reference cannot
    mutate a frozen definition in place.
    """
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    """Inverse of :func:`_freeze`: plain, mutable, JSON-safe payload."""
    if isinstance(value, MappingProxyType):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, frozenset, set)):
        return [_thaw(v) for v in value]
    return value


class NodeSourceTypeError(ValueError):
    """Raised for an unknown node source_type."""


SOURCE_TYPE_SUBSCRIPTION = "subscription"
SOURCE_TYPE_TXT = "txt"
SOURCE_TYPE_MANUAL = "manual"
SOURCE_TYPES = frozenset({
    SOURCE_TYPE_SUBSCRIPTION,
    SOURCE_TYPE_TXT,
    SOURCE_TYPE_MANUAL,
})


@dataclass(frozen=True)
class NodeSource:
    """Provenance of a node definition (no fetching logic lives here)."""

    source_id: str
    source_type: str = SOURCE_TYPE_MANUAL
    name: str = ""
    enabled: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source_type not in SOURCE_TYPES:
            raise NodeSourceTypeError(
                f"unknown source_type: {self.source_type!r} "
                f"(expected one of {sorted(SOURCE_TYPES)})"
            )
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    def to_dict(self) -> Dict[str, Any]:
        """Plain mutable JSON-safe payload (internal immutable views are
        never exposed)."""
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "name": self.name,
            "enabled": self.enabled,
            "metadata": _thaw(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NodeSource":
        return cls(
            source_id=data["source_id"],
            source_type=data.get("source_type", SOURCE_TYPE_MANUAL),
            name=data.get("name", ""),
            enabled=bool(data.get("enabled", True)),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True)
class NodeDefinition:
    """Static, persistable node definition — no runtime counters.

    Frozen so runtime code structurally cannot mutate an identity: a
    changed node means a NEW definition object (replacement semantics are
    a future persistence-policy concern).
    """

    node_id: str
    proxy: Dict[str, Any] = field(default_factory=dict)
    source_id: Optional[str] = None
    enabled: bool = True
    weight: int = 1
    max_concurrency: int = 8
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "proxy", _freeze(self.proxy))
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    def to_dict(self) -> Dict[str, Any]:
        """Plain mutable JSON-safe payload (internal immutable views are
        never exposed)."""
        return {
            "node_id": self.node_id,
            "proxy": _thaw(self.proxy),
            "source_id": self.source_id,
            "enabled": self.enabled,
            "weight": self.weight,
            "max_concurrency": self.max_concurrency,
            "metadata": _thaw(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NodeDefinition":
        return cls(
            node_id=data["node_id"],
            proxy=dict(data.get("proxy") or {}),
            source_id=data.get("source_id"),
            enabled=bool(data.get("enabled", True)),
            weight=int(data.get("weight", 1)),
            max_concurrency=int(data.get("max_concurrency", 8)),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class NodeRuntimeState:
    """Discardable runtime state of one node (keyed by ``node_id``).

    Nothing here is required for a NodeDefinition to exist: state can be
    dropped, rebuilt or zeroed at any time.  ``cooldown_until`` lives in
    the pool's monotonic clock domain and is only meaningful in-process.

    ``current_in_flight`` is live in-process accounting: it may be
    CAPTURED into a snapshot, but ``apply_to`` never restores it — a
    rebuilt node always starts with zero in-flight requests
    (ANON-006-FIX-01).
    """

    node_id: str
    current_in_flight: int = 0
    cooldown_until: float = 0.0
    consecutive_rate_limits: int = 0
    consecutive_failures: int = 0
    total_requests: int = 0
    total_failures: int = 0
    total_rate_limits: int = 0
    recaptcha_state: str = "unknown"  # unknown | ok | failed
    recaptcha_passes: int = 0
    recaptcha_failures: int = 0
    last_latency_s: Optional[float] = None
    last_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def capture(cls, node: Any) -> "NodeRuntimeState":
        """Read the runtime state of a live ExecutionNode."""
        return cls(
            node_id=node.node_id,
            current_in_flight=node.current_in_flight,
            cooldown_until=node.cooldown_until,
            consecutive_rate_limits=node.consecutive_rate_limits,
            consecutive_failures=node.consecutive_failures,
            total_requests=node.total_requests,
            total_failures=node.total_failures,
            total_rate_limits=node.total_rate_limits,
            recaptcha_state=node.recaptcha_state,
            recaptcha_passes=node.recaptcha_passes,
            recaptcha_failures=node.recaptcha_failures,
            last_latency_s=node.last_latency_s,
            last_error=node.last_error,
        )

    def apply_to(self, node: Any) -> None:
        """Write the inheritable runtime state onto an ExecutionNode.

        The caller owns the inheritance decision: applying a state object
        is the only channel through which counters move between nodes.

        ``current_in_flight`` is deliberately NOT restored: in-flight is
        live accounting of an actual running node and must never be
        inherited by a rebuild — a rebuilt node starts at zero.
        """
        node.cooldown_until = self.cooldown_until
        node.consecutive_rate_limits = self.consecutive_rate_limits
        node.consecutive_failures = self.consecutive_failures
        node.total_requests = self.total_requests
        node.total_failures = self.total_failures
        node.total_rate_limits = self.total_rate_limits
        node.recaptcha_state = self.recaptcha_state
        node.recaptcha_passes = self.recaptcha_passes
        node.recaptcha_failures = self.recaptcha_failures
        node.last_latency_s = self.last_latency_s
        node.last_error = self.last_error

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NodeRuntimeState":
        names = {f.name for f in fields(cls)}
        payload = {k: v for k, v in data.items() if k in names}
        return cls(**payload)
