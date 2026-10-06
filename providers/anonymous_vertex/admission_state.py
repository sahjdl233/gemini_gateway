"""Admission state read-view for the node pool (ANON-011-B).

Minimal read-only boundary between the admission world and the
AnonymousVertexNodePool::

    AdmissionStateProvider  (read-only Protocol consumed by the pool)
             ^
             |
    StoreAdmissionStateProvider  (adapter over an AdmissionResultStore)

The pool depends ONLY on the Protocol: it must never import the concrete
store.  The provider answers one question per node_id — "what is the
node's latest admission state?" — and never mutates anything.  A missing
state (``None``) means "never admitted", i.e. not a candidate.
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

