"""FastAPI entry point: builds the runtime from config and mounts the API.

TASK-001.5: main.py no longer knows about FakeProvider or FakeResource.
It reads config, initialises the ProviderRegistry, registers builtin
providers via bootstrap, and lets the registry create both providers and
their resources.  main.py stays agnostic to provider-specific types.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import FastAPI

from app.bootstrap import register_builtin_providers
from app.routes.chat import router as chat_router
from app.routes.models import router as models_router
from config.loader import default_config, load_config
from core.cooldown import CooldownManager
from core.model_registry import ModelRegistry
from core.pool import InMemoryPool
from core.provider_registry import ProviderRegistry, UnknownProviderError
from core.scheduler import Scheduler

logger = logging.getLogger(__name__)


def build_runtime(config: Dict[str, Any]) -> Scheduler:
    registry = ProviderRegistry()
    register_builtin_providers(registry)

    pools: Dict[str, Any] = {}
    providers: Dict[str, Any] = {}
    cooldown_cfg = config.get("scheduler", {}).get("cooldown", {})
    cooldown = CooldownManager(**cooldown_cfg)

    for provider_id, pcfg in config.get("providers", {}).items():
        if not pcfg.get("enabled", True):
            continue
        if not registry.has(provider_id):
            raise UnknownProviderError(
                "provider '" + provider_id + "' is not registered "
                "(no real Google access allowed in this phase)"
            )
        provider = registry.create(provider_id, pcfg)
        resources = registry.create_resources(
            provider_id, pcfg.get("resources", [])
        )
        providers[provider_id] = provider
        pool = InMemoryPool(
            provider=provider_id, resources=resources, cooldown=cooldown
        )
        # Providers may opt into a controlled, read-only resource view for
        # discovery. Antigravity uses this to discover models on a cold start;
        # the provider still does not own or create Resource objects.
        set_discovery_source = getattr(
            provider, "set_discovery_resource_source", None
        )
        if set_discovery_source is not None:
            set_discovery_source(lambda pool=pool: tuple(pool.resources))
        pools[provider_id] = pool

    model_registry = ModelRegistry(
        providers=providers,
        refresh_interval=config.get('model_registry', {}).get('refresh_interval', 300.0),
    )

    scheduler = Scheduler(
        providers=providers,
        pools=pools,
        model_registry=model_registry,
        max_retries=config.get('scheduler', {}).get('max_retries', 2),
    )

    logger.info(
        "runtime.built providers=%s models_refresh_interval=%.0fs",
        list(providers.keys()),
        model_registry.refresh_interval,
    )
    return scheduler


def create_app(config: Optional[Dict[str, Any]] = None) -> FastAPI:
    cfg = config if config is not None else default_config()
    scheduler = build_runtime(cfg)
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            for provider in scheduler.providers.values():
                close = getattr(provider, "close", None)
                if close is not None:
                    await close()

    app = FastAPI(
        title="Gemini Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.scheduler = scheduler
    app.state.config = cfg
    app.include_router(models_router)
    app.include_router(chat_router)

    @app.get("/", include_in_schema=False)
    async def root():
        return {
            "service": "gemini-gateway",
            "version": "0.1.0",
            "phase": "TASK-001",
            "providers": sorted(scheduler.providers.keys()),
        }

    return app


app = create_app()
