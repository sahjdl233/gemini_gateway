"""Execution backend layer: shared, provider-owned execution channels.

TASK-ARCH-003 (Phase 1): a Provider owns ONE backend, and that backend
owns ONE persistent execution channel (today: one httpx.AsyncClient).
Resources stay lightweight scheduling units and never own transport.
"""

from .base import ExecutionBackend
from .http import HttpExecutionBackend, HttpxClient

__all__ = ["ExecutionBackend", "HttpExecutionBackend", "HttpxClient"]
