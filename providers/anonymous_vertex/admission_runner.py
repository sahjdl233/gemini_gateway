"""Admission runner pipeline for Anonymous Vertex (ANON-008-B).

Execution layer between a NodeDefinition and an AdmissionChecker::

    NodeDefinition
          |
          v
    AdmissionRunner        (this module: lifecycle, timeout, conversion)
          |
          v
    AdmissionChecker       (Protocol, ANON-008-A)
          |
          v
    NodeAdmissionResult

The runner knows NOTHING about how a checker probes (HTTP, Vertex,
reCAPTCHA, proxy...) — it only guarantees:

* the checker is invoked under ``asyncio.wait_for(timeout_seconds)``;
* a checker exception NEVER escapes: it becomes ``FAILED /
  "checker_exception"``;
* a timeout becomes ``FAILED / "timeout"``;
* an invalid checker return (``None``, ``{}``, anything that is not a
  NodeAdmissionResult) becomes ``FAILED / "invalid_checker_result"``;
* every run is recorded as an immutable :class:`AdmissionAttempt` with
  timezone-aware UTC ``started_at`` / ``finished_at``.

One runner + one checker for now; composite checkers (Auth / Connectivity
/ Capability / Abuse) can wrap the same Protocol later without changing
this pipeline.  Cancellation is never swallowed: cancelling a caller
cancels the underlying check and no attempt is recorded.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from providers.anonymous_vertex.admission import (
    AdmissionChecker,
    NodeAdmissionResult,
    NodeAdmissionState,
)
from providers.anonymous_vertex.node_definitions import NodeDefinition

__all__ = ["AdmissionAttempt", "AdmissionRunner"]

DEFAULT_TIMEOUT_SECONDS = 30.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _failed(reason: str) -> NodeAdmissionResult:
    return NodeAdmissionResult(
        state=NodeAdmissionState.FAILED, reason=reason, checked_at=_utcnow()
    )


@dataclass(frozen=True)
class AdmissionAttempt:
    """One recorded check process (runtime event — never a definition)."""

    node_id: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    result: Optional[NodeAdmissionResult] = None


class AdmissionRunner:
    """Executes an AdmissionChecker under timeout and exception guards."""

    def __init__(
        self,
        checker: AdmissionChecker,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._checker = checker
        self._timeout_seconds = float(timeout_seconds)
        self._attempts: List[AdmissionAttempt] = []

    # -- introspection --

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def attempts(self) -> tuple:
        """Recorded attempts (append-only, oldest first)."""
        return tuple(self._attempts)

    # -- execution --

    async def run(self, node: NodeDefinition) -> NodeAdmissionResult:
        """Run one admission check for ``node``.

        Always returns a :class:`NodeAdmissionResult` — checker failures,
        timeouts and invalid returns are converted to FAILED results, and
        the attempt lifecycle (started_at / finished_at / result) is
        recorded.  ``asyncio.CancelledError`` propagates untouched.
        """
        started_at = _utcnow()
        result = await self._execute(node)
        finished_at = _utcnow()
        self._attempts.append(
            AdmissionAttempt(
                node_id=node.node_id,
                started_at=started_at,
                finished_at=finished_at,
                result=result,
            )
        )
        return result

    async def _execute(self, node: NodeDefinition) -> NodeAdmissionResult:
        try:
            result = await asyncio.wait_for(
                self._checker.check(node), timeout=self._timeout_seconds
            )
        except asyncio.TimeoutError:
            return _failed("timeout")
        except Exception as exc:  # noqa: BLE001 - conversion IS the contract
            return _failed(f"checker_exception: {exc}")

        if not isinstance(result, NodeAdmissionResult):
            return _failed("invalid_checker_result")
        return result
