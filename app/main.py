"FastAPI entry point: builds the runtime from config and mounts the API."

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import FastAPI

from app.routes.chat import router as chat_router
from app.routes.models import router as models_router
from config.loader import default_config, load_config
from core.cooldown import CooldownManager
from core.pool import InMemoryPool
from core.scheduler import Scheduler
from providers.fake import FakeProvider, FakeResource


def build_runtime(config: Dict[str, Any]) -> Scheduler:
    providers: Dict[str, Any] = {}
    pools: Dict[str, Any] = {}
    cooldown_cfg = config.get("scheduler", {}).get("cooldown", {})
    cooldown = CooldownManager(**cooldown_cfg)

    for provider_id, pcfg in config.get("providers", {}).items():
        if not pcfg.get("enabled", True):
            continue
        if provider_id != "fake":
            raise ValueError(
                f"provider '{provider_id}' is not implemented in TASK-000 "
                "(only 'fake' is available; no real Google access)"
            )
        provider = FakeProvider()
        resources = [
            FakeResource.model_validate(item) for item in pcfg.get("resources", [])
        ]
        pool = InMemoryPool(provider=provider_id, resources=resources, cooldown=cooldown)
        providers[provider_id] = provider
        pools[provider_id] = pool

    return Scheduler(providers=providers, pools=pools, max_retries=config.get("scheduler", {}).get("max_retries", 2))


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
            "phase": "TASK-000",
            "providers": sorted(scheduler.providers.keys()),
        }

    return app


app = create_app()
