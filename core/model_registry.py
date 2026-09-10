"""Model Registry with TTL-based refresh and single-flight protection.

Supports:
  - Lazy first build on first query
  - Manual refresh via model_registry.refresh()
  - TTL auto-refresh (configurable refresh_interval, default 300 s)
  - Concurrent refresh collapses into a single active refresh (single-flight)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from .errors import ProviderError
from .models import ModelInfo
from .provider import Provider

logger = logging.getLogger(__name__)


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
        self._last_refresh: Optional[float] = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._refresh_event: asyncio.Event = asyncio.Event()

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
        index: Dict[str, List[str]] = {}
        for provider_id, provider in self._providers.items():
            try:
                models = await provider.list_models()
            except ProviderError:
                logger.warning("model_registry.refresh.failed provider=%s", provider_id)
                continue
            for model in models:
                index.setdefault(model.id, []).append(provider_id)
        self._index = index
        self._last_refresh = time.monotonic()
        logger.info(
            "model_registry.refreshed models=%d providers=%d",
            len(self._index),
            len(self._providers),
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
