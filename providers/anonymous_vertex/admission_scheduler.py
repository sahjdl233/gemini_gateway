"""Admission periodic scheduler (ANON-010-B).

Background engine that periodically pushes every injected NodeDefinition
through the AdmissionOrchestrator and hands each AdmissionPipelineResult
to an injected async sink::

    NodeDefinitions
          |
          v
    AdmissionScheduler        (this module: lifecycle, concurrency, sink)
          |
          v
    AdmissionOrchestrator     (fail-fast pipeline, ANON-010-A)
          |
          v
    AdmissionPipelineResult --> result_sink(result)

Responsibilities: periodic triggering, background-task lifecycle,
concurrency limiting (asyncio.Semaphore), cancellation, result delivery.
NOT responsibilities: node storage, NodePool updates, node deletion,
quarantine decisions, checker creation — the sink consumer owns those.

Behaviour contract:

* the FIRST round runs immediately after ``start()`` (never one full
  interval late), then rounds repeat every ``interval_seconds``;
* at most ``max_concurrency`` admission checks run concurrently;
* a sink failure on one node is recorded and isolated — it must never
  stop the scheduler or the other nodes' deliveries; checker/orchestrator
  exceptions are likewise isolated per node;
* ``asyncio.CancelledError`` is never swallowed: ``stop()`` cancels the
  background task, awaits its completion, and leaves no residual tasks;
* ``start()`` while running raises RuntimeError (no double loops);
  ``stop()`` is idempotent and safe before any ``start()``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Sequence

from providers.anonymous_vertex.admission_orchestrator import (
    AdmissionOrchestrator,
)
from providers.anonymous_vertex.node_definitions import NodeDefinition

__all__ = ["AdmissionScheduler"]

logger = logging.getLogger(__name__)

Sink = Callable[[Any], Awaitable[None]]


class AdmissionScheduler:
    """Periodically re-checks nodes through the admission pipeline."""

    def __init__(
        self,
        orchestrator: AdmissionOrchestrator,
        nodes: Sequence[NodeDefinition],
        *,
        interval_seconds: float = 300.0,
        max_concurrency: int = 5,
        result_sink: Sink,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self._orchestrator = orchestrator
        self._nodes = tuple(nodes)
        self._interval_seconds = float(interval_seconds)
        self._result_sink = result_sink
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._task: asyncio.Task | None = None
        self._running = False
        self._stats = {"rounds": 0, "results": 0, "sink_errors": 0,
                       "check_errors": 0}

    # -- introspection --

    @property
    def running(self) -> bool:
        return self._running

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    @property
    def nodes(self) -> tuple:
        return self._nodes

    @property
    def stats(self) -> dict:
        """Delivery / failure counters since construction (observability)."""
        return dict(self._stats)

    # -- lifecycle --

    async def start(self) -> None:
        """Start the background loop (first round runs immediately)."""
        if self._running:
            raise RuntimeError("admission scheduler is already running")
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Cancel the background loop and wait for it; idempotent."""
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._running = False

    # -- engine --

    async def _run_loop(self) -> None:
        try:
            while True:
                await self._run_round()
                await asyncio.sleep(self._interval_seconds)
        except asyncio.CancelledError:
            raise  # stop() owns cancellation; no conversion
        finally:
            self._running = False

    async def _run_round(self) -> None:
        """One full pass over all nodes (bounded by the semaphore)."""

        async def check_one(node: NodeDefinition) -> None:
            async with self._semaphore:
                result = await self._orchestrator.check(node)
            try:
                await self._result_sink(result)
                self._stats["results"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolation by contract
                self._stats["sink_errors"] += 1
                logger.warning(
                    "admission_scheduler.sink_error node=%s error=%s",
                    node.node_id,
                    exc,
                )

        wrapped = []
        for node in self._nodes:
            async def _guarded(node: NodeDefinition = node) -> None:
                try:
                    await check_one(node)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - isolation
                    self._stats["check_errors"] += 1
                    logger.warning(
                        "admission_scheduler.check_error node=%s error=%s",
                        node.node_id,
                        exc,
                    )

            wrapped.append(_guarded())
        if wrapped:
            await asyncio.gather(*wrapped)
        self._stats["rounds"] += 1
