"""Admission result sink: fan-out bridge (ANON-011-C).

The thin adapter that completes the ANON-011-C architecture::

    AdmissionScheduler
          |
          v
    AdmissionPipelineResult
          |---> AdmissionResultStore.put(result)          (facts)
          '---> NodePool.update_admission_state(...)      (push)

The Scheduler still knows neither the store nor the pool; the store still
knows neither the scheduler nor the pool.  This bridge is the ONLY place
where one admission result fans out to both consumers.

Error semantics:

* a STORE failure must never swallow the pool update -- the pool push is
  always attempted, and the store error is re-raised afterwards so the
  caller (the scheduler's per-node isolation) still sees the delivery
  failure;
* a POOL push failure (e.g. unknown node_id) propagates immediately;
* ``asyncio.CancelledError`` is never captured.
"""

from __future__ import annotations

import logging
from typing import Any

__all__ = ["AdmissionResultSink"]

logger = logging.getLogger(__name__)


class AdmissionResultSink:
    """Deliver one admission pipeline result to store AND node pool."""

    def __init__(self, store: Any, node_pool: Any) -> None:
        # Both consumers stay duck-typed (AdmissionResultStore protocol /
        # AnonymousVertexNodePool.update_admission_state) so this module
        # imports neither concrete type.
        self._store = store
        self._node_pool = node_pool

    async def __call__(self, result: Any) -> None:
        node_id = result.node_id
        state = result.final_result.state

        store_error = None
        try:
            await self._store.put(result)
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            store_error = exc
            logger.warning(
                "admission_sink.store_error node=%s error=%s", node_id, exc
            )

        # Always push to the pool, even when the store write failed.
        await self._node_pool.update_admission_state(node_id, state)

        if store_error is not None:
            raise store_error

    @property
    def store(self) -> Any:
        return self._store

    @property
    def node_pool(self) -> Any:
        return self._node_pool
