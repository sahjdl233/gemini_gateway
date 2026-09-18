"""Scheduler: resource selection, health-aware routing, cooldown, retry, fallback.

TASK-000 rule: the Scheduler NEVER parses raw Google errors. Provider
adapters translate everything into core.errors.ProviderError types.
"""

from __future__ import annotations

import logging
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
from .model_registry import ModelRegistry
from .pool import ResourcePool
from .provider import Provider

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        *,
        providers: Dict[str, Provider],
        pools: Dict[str, ResourcePool],
        model_registry: Optional[ModelRegistry] = None,
        max_retries: int = 2,
    ) -> None:
        self.providers = providers
        self.pools = pools
        self.max_retries = max_retries
        self._model_registry = model_registry or ModelRegistry(providers=providers)

    @property
    def model_registry(self) -> ModelRegistry:
        return self._model_registry

    async def rebuild_model_index(self) -> None:
        await self._model_registry.refresh()

    async def list_models(self) -> List[ModelInfo]:
        return await self._model_registry.list_models()

    def _candidate_pools(self, model: str) -> List[ResourcePool]:
        provider_ids = self._model_registry.snapshot().get(model, [])
        return [self.pools[pid] for pid in provider_ids if pid in self.pools]

    def _raise_no_pool(self, model: str) -> None:
        raise ModelNotFoundError(
            f"model '{model}' is not offered by any enabled provider",
            provider="gateway",
        )

    async def _raise_when_nothing_acquired(
        self,
        pools: List[ResourcePool],
        tried: Set,
        errors: List[ProviderError],
    ) -> None:
        if errors:
            raise errors[-1]
        raise UpstreamUnavailableError(
            "no usable resource currently available", provider="gateway"
        )

    async def chat_completion(self, request: ChatRequest) -> ChatResponse:
        await self._model_registry._ensure_fresh()
        pools = self._candidate_pools(request.model)
        if not pools:
            self._raise_no_pool(request.model)

        errors: List[ProviderError] = []
        tried: Set = set()

        for _ in range(self.max_retries + 1):
            acquired_any = False
            for pool in pools:
                resource = await pool.acquire(skip=tried)
                if resource is None:
                    continue
                acquired_any = True
                tried.add(resource.resource_key)
                provider = self.providers[resource.provider]
                logger.info('scheduler.acquire provider=%s resource=%s model=%s', resource.provider, str(resource.resource_key), request.model)
                try:
                    response = await provider.complete(request, resource)
                except ProviderError as exc:
                    logger.warning('scheduler.error provider=%s resource=%s model=%s error=%s', resource.provider, str(resource.resource_key), request.model, type(exc).__name__)
                    try:
                        if isinstance(exc, RateLimitError):
                            await pool.record_rate_limit(resource, exc.retry_after)
                        else:
                            await pool.record_failure(resource, exc)
                    finally:
                        await pool.release(resource)
                    errors.append(exc)
                    if not is_retryable(exc):
                        raise exc
                    continue
                logger.info('scheduler.success provider=%s resource=%s model=%s', resource.provider, str(resource.resource_key), request.model)
                await pool.record_success(resource)
                await pool.release(resource)
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
        logger.info('scheduler.stream model=%s', request.model)
        await self._model_registry._ensure_fresh()
        pools = self._candidate_pools(request.model)
        if not pools:
            self._raise_no_pool(request.model)

        errors: List[ProviderError] = []
        tried: Set = set()

        for _ in range(self.max_retries + 1):
            acquired_any = False
            for pool in pools:
                resource = await pool.acquire(skip=tried)
                if resource is None:
                    continue
                acquired_any = True
                tried.add(resource.resource_key)
                provider = self.providers[resource.provider]
                sent_any = False
                logger.info('scheduler.stream.acquire provider=%s resource=%s model=%s', resource.provider, str(resource.resource_key), request.model)
                try:
                    async for chunk in provider.stream(request, resource):
                        sent_any = True
                        yield chunk
                except ProviderError as exc:
                    logger.warning('scheduler.stream.error provider=%s resource=%s model=%s error=%s', resource.provider, str(resource.resource_key), request.model, type(exc).__name__)
                    try:
                        if isinstance(exc, RateLimitError):
                            await pool.record_rate_limit(resource, exc.retry_after)
                        else:
                            await pool.record_failure(resource, exc)
                    finally:
                        await pool.release(resource)
                    errors.append(exc)
                    if sent_any or not is_retryable(exc):
                        raise exc
                    continue
                await pool.record_success(resource)
                await pool.release(resource)
                return

            if not acquired_any:
                await self._raise_when_nothing_acquired(pools, tried, errors)
                break

        if errors:
            raise errors[-1]
        raise UpstreamUnavailableError("no usable resource", provider="gateway")
