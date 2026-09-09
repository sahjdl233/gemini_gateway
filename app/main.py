"""FastAPI entry point: builds the runtime from config and mounts the API."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI

from app.routes.chat import router as chat_router
from app.routes.models import router as models_router
from config.loader import default_config, load_config
from core.cooldown import CooldownManager
from core.model_registry import ModelRegistry
from core.pool import InMemoryPool
from core.provider_registry import ProviderRegistry
from core.scheduler import Scheduler
from providers.fake import FakeProvider, FakeResource

logger = logging.getLogger(__name__)


# Provider factory map -- add new providers here, never in an if/elif chain.
_PROVIDER_FACTORIES = {
    "fake": FakeProvider,
}


def build_runtime(config: Dict[str, Any]) -> Scheduler:
    registry = ProviderRegistry()
    pools: Dict[str, Any] = {}
    providers: Dict[str, Any] = {}
    cooldown_cfg = config.get("scheduler", {}).get("cooldown", {})
    cooldown = CooldownManager(**cooldown_cfg)

    for provider_id, pcfg in config.get("providers", {}).items():
        if not pcfg.get("enabled", True):
            continue
        factory = _PROVIDER_FACTORIES.get(provider_id)
        if factory is None:
            raise ValueError(
                f"provider '{provider_id}' is not registered in _PROVIDER_FACTORIES "
                "(no real Google access allowed in this phase)"
            )
        provider = factory()
        registry.register(provider_id, lambda: provider)
        providers[provider_id] = provider
        resources = [
            FakeResource.model_validate(item) for item in pcfg.get("resources", [])
        ]
        pool = InMemoryPool(provider=provider_id, resources=resources, cooldown=cooldown)
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
    app = FastAPI(title="Gemini Gateway", version="0.1.0")
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