"""Model Registry with TTL-based refresh and single-flight protection.

Supports:
  - Lazy first build on first query
  - Manual refresh via model_registry.refresh()
  - TTL auto-refresh (configurable refresh_interval, default 300 s)
  - Concurrent refresh collapses into a single active refresh (single-flight)

Discovery failure semantics (TASK-MODEL-001):
  A provider whose Discovery fails is never allowed to empty itself out of
  the index. The registry keeps that provider's last *successful* model list
  and records the failure separately, so a transient network blip can never
  turn into a client-visible model-not-found / 404.

  - Discovery succeeds -> that provider's model list is replaced. An empty
    result is a real answer ("this provider currently offers nothing") and
    does clear the previous list.
  - Discovery fails    -> the previous successful list is kept. If there
    never was one, the provider contributes no models *and* is flagged via
    `failures` / `provider_status()`, so "Discovery failed" stays
    distinguishable from a genuine empty discovery.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .errors import ProviderError
from .models import ModelInfo
from .provider import Provider

logger = logging.getLogger(__name__)


@dataclass
class ProviderDiscoveryState:
    """Last known good Discovery result for a single provider."""

    provider_id: str
    # Model ids from the most recent *successful* Discovery. An empty list is
    # a valid answer, but only once a success has actually been recorded.
    models: List[str] = field(default_factory=list)
    last_success: Optional[float] = None
    last_error: Optional[str] = None
    last_failure: Optional[float] = None
    success_count: int = 0
    failure_count: int = 0

    @property
    def discovered(self) -> bool:
        """True once Discovery has succeeded at least once.

        This is what separates a real empty discovery from a provider whose
        Discovery has never worked: both expose ``models == []``, but only the
        latter has ``discovered is False`` together with a ``last_error``.
        """
        return self.last_success is not None

    @property
    def failed(self) -> bool:
        """True when the most recent Discovery attempt for this provider failed."""
        return self.failure_count > 0 and (
            self.last_failure is not None
            and (self.last_success is None or self.last_failure >= self.last_success)
        )


class ModelRegistry:
    def __init__(
        self,
        *,
        providers: Dict[str, Provider],
        refresh_interval: float = 300.0,
    ) -> None:
        self._providers = providers
        self._refresh_interval = refresh_interval
        self._index: Dict[str, List[str]] = {}
        # Per-provider last known good Discovery results. The index is derived
        # from these on every refresh so that a provider failing Discovery
        # keeps contributing its previous models instead of vanishing.
        self._states: Dict[str, ProviderDiscoveryState] = {
            pid: ProviderDiscoveryState(provider_id=pid) for pid in providers
        }
        self._last_refresh: Optional[float] = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._refresh_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Discovery status introspection (TASK-MODEL-001)
    # ------------------------------------------------------------------
    @property
    def failures(self) -> Dict[str, str]:
        """Map of provider_id -> last Discovery error message.

        Only providers whose *most recent* Discovery attempt failed are
        present. This is the operator-visible difference between "this
        provider offers no models" and "we could not ask it".
        """
        return {
            pid: state.last_error
            for pid, state in self._states.items()
            if state.failed and state.last_error
        }

    def provider_status(self, provider_id: str) -> Dict[str, Any]:
        """Per-provider Discovery status snapshot for debugging / admin."""
        state = self._states.get(provider_id)
        if state is None:
            return {
                "provider": provider_id,
                "known": False,
                "discovered": False,
                "failed": False,
                "models": 0,
                "last_error": None,
            }
        return {
            "provider": provider_id,
            "known": True,
            "discovered": state.discovered,
            "failed": state.failed,
            "models": len(state.models),
            "last_error": state.last_error,
            "success_count": state.success_count,
            "failure_count": state.failure_count,
        }

    def discovery_status(self) -> Dict[str, Dict[str, Any]]:
        """Discovery status for every known provider."""
        return {pid: self.provider_status(pid) for pid in self._states}

    @property
    def refresh_interval(self) -> float:
        return self._refresh_interval

    @property
    def last_refresh(self) -> Optional[float]:
        return self._last_refresh

    def _is_expired(self) -> bool:
        # A never-refreshed registry is always stale: force the very first
        # query to perform Discovery even when time.monotonic() is smaller
        # than the refresh interval (TASK-002-FIX-02).
        if self._last_refresh is None:
            return True
        return (time.monotonic() - self._last_refresh) >= self._refresh_interval

    async def refresh(self) -> Dict[str, List[str]]:
        """Rebuild the model index from all providers.

        Multiple concurrent callers will block on a single lock and see the
        same refreshed data (single-flight behaviour).
        """
        async with self._lock:
            await self._do_refresh()
        return dict(self._index)

    async def _ensure_fresh(self) -> None:
        """Called before every query; triggers refresh only once per TTL."""
        if not self._is_expired():
            return
        async with self._lock:
            if not self._is_expired():
                return
            await self._do_refresh()

    async def _do_refresh(self) -> None:
        """Rebuild the model index from per-provider last known good state.

        TASK-MODEL-001: a provider that fails Discovery keeps its previous
        successful model list. The index is then rebuilt from the *merged*
        per-provider state, so a transient Discovery failure can never remove
        models that were previously known to exist.
        """
        for provider_id, provider in self._providers.items():
            state = self._states.setdefault(
                provider_id, ProviderDiscoveryState(provider_id=provider_id)
            )
            try:
                models = list(await provider.list_models() or [])
            except ProviderError as exc:
                # Keep the previously discovered models for this provider and
                # record the failure. We deliberately do NOT clear the entry.
                state.last_error = str(exc) or exc.__class__.__name__
                state.last_failure = time.monotonic()
                state.failure_count += 1
                if state.discovered:
                    logger.warning(
                        "model_registry.refresh.failed provider=%s "
                        "error=%s; keeping %d stale model(s)",
                        provider_id,
                        state.last_error,
                        len(state.models),
                    )
                else:
                    # First-ever Discovery failed: the provider contributes no
                    # models, but the failure is recorded so it stays
                    # distinguishable from a genuine empty discovery.
                    logger.warning(
                        "model_registry.refresh.failed provider=%s error=%s; "
                        "no previous Discovery to fall back on",
                        provider_id,
                        state.last_error,
                    )
                continue
            except Exception as exc:  # noqa: BLE001 - defensive isolation
                # Some adapters (e.g. Antigravity) can surface a non
                # ProviderError during Discovery (e.g. RuntimeError when a
                # backend is missing). Treat it like any other Discovery
                # failure so one bad provider cannot abort the whole refresh
                # and leave the registry permanently stuck on a stale index.
                state.last_error = str(exc) or exc.__class__.__name__
                state.last_failure = time.monotonic()
                state.failure_count += 1
                logger.warning(
                    "model_registry.refresh.failed provider=%s "
                    "error_type=%s error=%s; keeping %d stale model(s)",
                    provider_id,
                    exc.__class__.__name__,
                    state.last_error,
                    len(state.models),
                )
                continue

            # Discovery succeeded. An empty list here is a genuine "this
            # provider currently exposes no models" answer, and it correctly
            # replaces the previous list.
            state.models = [m.id for m in models]
            state.last_success = time.monotonic()
            state.last_error = None
            state.success_count += 1

        # Rebuild the index from merged per-provider state.
        index: Dict[str, List[str]] = {}
        for provider_id, state in self._states.items():
            # Only providers that have ever succeeded contribute models. A
            # provider that has never completed Discovery has models == []
            # anyway, so this loop is simply "publish the last known good".
            for model_id in state.models:
                index.setdefault(model_id, []).append(provider_id)
        self._index = index
        self._last_refresh = time.monotonic()
        logger.info(
            "model_registry.refreshed models=%d providers=%d failures=%s",
            len(self._index),
            len(self._providers),
            sorted(self.failures) or "none",
        )

    async def providers_for(self, model: str) -> List[str]:
        await self._ensure_fresh()
        return list(self._index.get(model, []))

    async def list_models(self) -> List[ModelInfo]:
        await self._ensure_fresh()
        result: List[ModelInfo] = []
        for model_id, provider_ids in self._index.items():
            for pid in provider_ids:
                result.append(
                    ModelInfo(id=model_id, provider=pid)
                )
        return result

    def snapshot(self) -> Dict[str, List[str]]:
        """Return current index without triggering refresh (read-only)."""
        return dict(self._index)
