"""External admission read-model adapter (ANON-011-B, re-scoped 011-C).

The pool's admission gate is now a LOCAL projection fed by pushes
(``AnonymousVertexNodePool.update_admission_state``); the pool never
queries anything here at acquire time.  This module keeps the read-only
lookup abstraction for its remaining role: EXTERNAL consumers and
bootstrap flows that need to read admission state from a result store
(e.g. seeding a projection from persisted results, admin/observability
read models).

    AdmissionResultStore  --(adapter)-->  get_state(node_id)

Read-only by contract: nothing here mutates a store, a node or a pool.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from providers.anonymous_vertex.admission import NodeAdmissionState

__all__ = ["AdmissionStateProvider", "StoreAdmissionStateProvider"]


@runtime_checkable
class AdmissionStateProvider(Protocol):
    """Read-only admission state lookup by node_id."""

    async def get_state(self, node_id: str) -> Optional[NodeAdmissionState]:
        """Latest admission state of ``node_id``; None if never checked."""
        ...


class StoreAdmissionStateProvider:
    """Adapter exposing an AdmissionResultStore as a state provider.

    Pure projection: the store's latest result -> its final state.  No
    caching, no mutation, no policy involvement.
    """

    def __init__(self, store: Any) -> None:
        # ``store`` is any object satisfying the AdmissionResultStore
        # protocol; importing the store class here would couple the two
        # responsibilities, so it stays duck-typed.
        self._store = store

    async def get_state(self, node_id: str) -> Optional[NodeAdmissionState]:
        result = await self._store.get(node_id)
        if result is None:
            return None
        return result.final_result.state

