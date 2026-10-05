"""Admission orchestrator: fail-fast multi-check pipeline (ANON-010-A).

Single entry point for the admission chain::

    NodeDefinition
          |
          v
    AdmissionOrchestrator
          |    (strict order, fail-fast)
          +--> checker[0]   e.g. ConnectivityChecker
          +--> checker[1]   e.g. CapabilityChecker
          +--> checker[2]   e.g. AuthChecker
          |
          v
    AdmissionPipelineResult

Responsibilities: checker ordering, fail-fast, attempt recording, final
result aggregation.  NOT responsibilities: checker internals, quarantine
decisions, NodePool, persistence.

Rules:

* checkers run STRICTLY in the injected order; none are created here and
  no concrete checker type is imported;
* a FAILED result stops the pipeline immediately — later checkers never
  run, so a node that cannot even connect is never capability-probed;
* ``final_result is attempts[-1]`` ALWAYS: the original checker output
  object is preserved verbatim (no reason splicing, no re-wrapping, no
  checked_at loss);
* exceptions are NOT swallowed — wrapping a checker in an
  AdmissionRunner (ANON-008-B) is how timeouts / exceptions get converted;
  ``asyncio.CancelledError`` propagates untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, Tuple

from providers.anonymous_vertex.admission import (
    AdmissionChecker,
    NodeAdmissionResult,
    NodeAdmissionState,
)
from providers.anonymous_vertex.node_definitions import NodeDefinition

__all__ = ["AdmissionPipelineResult", "AdmissionOrchestrator"]


@dataclass(frozen=True)
class AdmissionPipelineResult:
    """Aggregated outcome of one orchestrated admission check."""

    node_id: str
    final_result: NodeAdmissionResult
    attempts: Tuple[NodeAdmissionResult, ...]


class AdmissionOrchestrator:
    """Runs injected checkers in order and fails fast."""

    def __init__(self, checkers: Sequence[AdmissionChecker]) -> None:
        checkers = tuple(checkers)
        if not checkers:
            raise ValueError(
                "admission pipeline requires at least one checker"
            )
        self._checkers = checkers

    @property
    def checkers(self) -> tuple:
        """The injected checkers, in strict execution order."""
        return self._checkers

    async def check(self, node: NodeDefinition) -> AdmissionPipelineResult:
        """Run the pipeline for ``node`` (fail-fast on FAILED)."""
        attempts: list = []
        for checker in self._checkers:
            result = await checker.check(node)
            attempts.append(result)
            if result.state == NodeAdmissionState.FAILED:
                break
        final_result = attempts[-1]
        return AdmissionPipelineResult(
            node_id=node.node_id,
            final_result=final_result,
            attempts=tuple(attempts),
        )

