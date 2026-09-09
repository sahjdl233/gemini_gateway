"""Scheduler: resource selection, health-aware routing, cooldown, retry,
fallback.

TASK-000 rule: the Scheduler NEVER parses raw Google errors. Provider
adapters translate everything into core.errors.ProviderError types.
"""

from __future__ import annotations

from typing import AsyncGenerator, Dict, List, Optional, Set

from .errors import (
    ModelNotFoundError,
    ProviderError,
    RateLimitError,
    UpstreamUnavailableError,
    is_retryable,
)
from .health import HealthState
from .models import ChatChunk, ChatRequest, ChatResponse, ModelInfo
from .pool import ResourcePool
from .provider import Provider


class Scheduler:
    def __init__(
        self,
        *,
        providers: Dict[str, Provider],
        pools: Dict[str, ResourcePool],
        max_retries: int = 2,
    ) -> None:
        self.providers = providers
        self.pools = pools
        self.max_retries = max_retries
        self._model_index: Dict[str, List[str]] = {}
        self._model_index_ready = False

    async def rebuild_model_index(self) -> None:
        index: Dict[str, List[str]] = {}
        for provider_id, provider in self.providers.items():
            try:
                models = await provider.list_models()
            except ProviderError:
                continue
            for model in models:
                index.setdefault(model.id, []).append(provider_id)
        self._model_index = index
        self._model_index_ready = True

    async def _ensure_model_index(self) -> None:
        if not self._model_index_ready:
            await self.rebuild_model_index()

    async def list_models(self) -> List[ModelInfo]:
        result: List[ModelInfo] = []
        for _, provider in self.providers.items():
            try:
                result.extend(await provider.list_models())
            except ProviderError:
                continue
        return result

    def _candidate_pools(self, model: str) -> List[ResourcePool]:
        provider_ids = self._model_index.get(model, [])
        return [self.pools[pid] for pid in provider_ids if pid in self.pools]

    def _raise_no_pool(self, model: str) -> None:
        raise ModelNotFoundError(
            f"model '{model}' is not offered by any enabled provider",
            provider="gateway",
        )

    async def _raise_when_nothing_acquired(
        self,
        pools: List[ResourcePool],
        tried: Set[str],
        errors: List[ProviderError],
    ) -> None:
        if errors:
            raise errors[-1]
        raise UpstreamUnavailableError(
            "no usable resource currently available", provider="gateway"
        )

    async def chat_completion(self, request: ChatRequest) -> ChatResponse:
        await self._ensure_model_index()
        pools = self._candidate_pools(request.model)
        if not pools:
            self._raise_no_pool(request.model)

        errors: List[ProviderError] = []
        tried: Set[str] = set()

        for _ in range(self.max_retries + 1):
            acquired_any = False
            for pool in pools:
                resource = await pool.acquire(skip=tried)
                if resource is None:
                    continue
                acquired_any = True
                tried.add(resource.id)
                provider = self.providers[resource.provider]
                try:
                    try:
                        response = await provider.complete(request, resource)
                    finally:
                        await pool.release(resource)
                except ProviderError as exc:
                    if isinstance(exc, RateLimitError):
                        await pool.record_rate_limit(resource, exc.retry_after)
                    else:
                        await pool.record_failure(resource, exc)
                    errors.append(exc)
                    if not is_retryable(exc):
                        raise exc
                    continue
                await pool.record_success(resource)
                return response

            if not acquired_any:
                await self._raise_when_nothing_acquired(pools, tried, errors)
                break

        if errors:
            raise errors[-1]
        raise UpstreamUnavailableError("no usable resource", provider="gateway")

    async def stream_chat(
        self, request: ChatRequest
    ) -> AsyncGenerator[ChatChunk, None]:
        await self._ensure_model_index()
        pools = self._candidate_pools(request.model)
        if not pools:
            self._raise_no_pool(request.model)

        errors: List[ProviderError] = []
        tried: Set[str] = set()

        for _ in range(self.max_retries + 1):
            acquired_any = False
            for pool in pools:
                resource = await pool.acquire(skip=tried)
                if resource is None:
                    continue
                acquired_any = True
                tried.add(resource.id)
                provider = self.providers[resource.provider]
                sent_any = False
                try:
                    try:
                        async for chunk in provider.stream(request, resource):
                            sent_any = True
                            yield chunk
                    finally:
                        await pool.release(resource)
                except ProviderError as exc:
                    if isinstance(exc, RateLimitError):
                        await pool.record_rate_limit(resource, exc.retry_after)
                    else:
                        await pool.record_failure(resource, exc)
                    errors.append(exc)
                    if sent_any or not is_retryable(exc):
                        raise exc
                    continue
                await pool.record_success(resource)
                return

            if not acquired_any:
                await self._raise_when_nothing_acquired(pools, tried, errors)
                break

        if errors:
            raise errors[-1]
        raise UpstreamUnavailableError("no usable resource", provider="gateway")
