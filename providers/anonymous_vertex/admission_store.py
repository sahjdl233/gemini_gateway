"""In-memory admission result store (ANON-011-A).

Records admission facts and serves health snapshots::

    AdmissionPipelineResult (from the scheduler's sink)
          |
          v
    AdmissionResultStore
          |
          +--> get(node_id)        latest result per node
          +--> history(node_id)    full append-only per-node history
          +--> snapshot()          every node's latest result
          +--> health_snapshot()   lightweight NodeHealthSnapshot view

Boundary rules:

* the store records FACTS only — no FAILED->QUARANTINED conversion, no
  AdmissionPolicy calls (the policy already did its job upstream), no node
  deletion, no NodePool sync, no persistence;
* nothing mutable is exposed: every read returns a freshly built tuple,
  and the stored objects are themselves immutable dataclasses;
* all state lives behind one ``asyncio.Lock`` per store instance — no
  globals — so concurrent ``put`` calls cannot corrupt latest or history.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Protocol, runtime_checkable, Tuple

from providers.anonymous_vertex.admission import NodeAdmissionState
from providers.anonymous_vertex.admission_orchestrator import (
    AdmissionPipelineResult,
)

__all__ = [
    "AdmissionResultStore",
    "InMemoryAdmissionResultStore",
    "NodeHealthSnapshot",
]


@runtime_checkable
class AdmissionResultStore(Protocol):
    """Async storage contract for admission pipeline results."""

    async def put(self, result: AdmissionPipelineResult) -> None:
        """Record one pipeline result (latest overwrite + history append)."""
        ...

    async def get(self, node_id: str) -> Optional[AdmissionPipelineResult]:
        """Latest recorded result for ``node_id``, or None."""
        ...

    async def snapshot(self) -> Tuple[AdmissionPipelineResult, ...]:
        """Latest result of every known node (insertion order)."""
        ...

    async def history(
        self, node_id: str
    ) -> Tuple[AdmissionPipelineResult, ...]:
        """All recorded results for ``node_id``, in insertion order."""
        ...


@dataclass(frozen=True)
class NodeHealthSnapshot:
    """Lightweight health view of one node's latest admission outcome."""

    node_id: str
    state: NodeAdmissionState
    reason: Optional[str]
    checked_at: datetime


class InMemoryAdmissionResultStore:
    """Per-store in-memory implementation (no globals, no persistence)."""

    def __init__(self) -> None:
        self._latest: Dict[str, AdmissionPipelineResult] = {}
        self._history: Dict[str, List[AdmissionPipelineResult]] = {}
        self._lock = asyncio.Lock()

    # -- write path --

    async def put(self, result: AdmissionPipelineResult) -> None:
        async with self._lock:
            node_id = result.node_id
            self._latest[node_id] = result
            history = self._history.setdefault(node_id, [])
            history.append(result)

    # -- read paths (tuples only; internal lists never escape) --

    async def get(self, node_id: str) -> Optional[AdmissionPipelineResult]:
        async with self._lock:
            return self._latest.get(node_id)

    async def snapshot(self) -> Tuple[AdmissionPipelineResult, ...]:
        async with self._lock:
            return tuple(self._latest.values())

    async def history(
        self, node_id: str
    ) -> Tuple[AdmissionPipelineResult, ...]:
        async with self._lock:
            return tuple(self._history.get(node_id, ()))

    # -- derived views --

    async def health_snapshot(self) -> Tuple[NodeHealthSnapshot, ...]:
        """Project every node's latest result into a health snapshot."""
        async with self._lock:
            return tuple(
                NodeHealthSnapshot(
                    node_id=result.node_id,
                    state=result.final_result.state,
                    reason=result.final_result.reason,
                    checked_at=result.final_result.checked_at,
                )
                for result in self._latest.values()
            )
