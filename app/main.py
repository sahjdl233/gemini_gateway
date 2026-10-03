"""FastAPI entry point: builds the runtime from config and mounts the API.

TASK-001.5: main.py no longer knows about FakeProvider or FakeResource.
It reads config, initialises the ProviderRegistry, registers builtin
providers via bootstrap, and lets the registry create both providers and
their resources.  main.py stays agnostic to provider-specific types.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import FastAPI

from app.bootstrap import register_builtin_providers
from app.routes.chat import router as chat_router
from app.routes.models import router as models_router
from app.routes.admin import mount_admin_assets
from app.routes.admin import router as admin_router
from app.management import ResourceManager
from config.loader import default_config, load_config
from core.cooldown import CooldownManager
from core.resource_bootstrap import ResourceBootstrapService
from core.resource_definition_repository import (
    ResourceRepositoryDefinitionSource,
)
from core.resource_definition_repository_factory import (
    create_resource_definition_repository,
    resource_bootstrap_settings,
)
from core.resource_repository_memory import MemoryResourceRepository
from core.runtime_resource_factory import create_runtime_resources
from core.credential import (
    Credential,
    CredentialRepository,
    CredentialStore,
    DuplicateCredentialError,
)
from core.model_registry import ModelRegistry
from core.pool import InMemoryPool
from core.provider_registry import ProviderRegistry, UnknownProviderError
from core.scheduler import Scheduler

logger = logging.getLogger(__name__)


def build_credential_store(config: Dict[str, Any]) -> CredentialRepository:
    """Build the credential repository from config (AUTH-002/AUTH-009).

    Default (and default-when-unconfigured) backend is the in-memory
    ``CredentialStore``.  Explicitly selecting
    ``credential_repository.backend: postgres`` builds the durable
    repository: the database URL comes from
    ``GEMINI_GATEWAY_DATABASE_URL``, the payload encryption key from
    ``GEMINI_GATEWAY_ENCRYPTION_KEY`` (AUTH-007), and ANY failure —
    missing configuration, unreachable database, schema error —
    propagates as a startup failure.  A silent fallback to an empty
    in-memory store is deliberately not implemented: infrastructure
    failure must not masquerade as credential-not-found.
    """
    repo_cfg = config.get("credential_repository") or {}
    backend = str(repo_cfg.get("backend", "memory")).lower()
    if backend == "memory":
        store = CredentialStore()
        for entry in config.get("credentials") or []:
            store.add(Credential.model_validate(entry))
        return store
    if backend == "postgres":
        return _build_postgres_credential_repository(config)
    raise ValueError(f"unknown credential repository backend: {backend!r}")


def _build_postgres_credential_repository(
    config: Dict[str, Any],
) -> CredentialRepository:
    import os

    from core.credential_encryption import CredentialEncryptor
    from core.credential_postgres import (
        PostgreSQLCredentialRepository,
        psycopg_connection_factory,
    )

    dsn = os.environ.get("GEMINI_GATEWAY_DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "credential_repository.backend=postgres requires the "
            "GEMINI_GATEWAY_DATABASE_URL environment variable"
        )
    encryptor = CredentialEncryptor.from_environment()
    repository = PostgreSQLCredentialRepository(
        psycopg_connection_factory(dsn), encryptor=encryptor
    )
    repository.initialize()

    for entry in config.get("credentials") or []:
        credential = Credential.model_validate(entry)
        try:
            repository.add(credential)
        except DuplicateCredentialError:
            logger.info("credential.already_persisted id=%s", credential.id)
    return repository


def build_runtime(
    config: Dict[str, Any],
    credential_store: Optional[CredentialRepository] = None,
    *,
    resource_definitions: Optional[List[Any]] = None,
) -> Scheduler:
    """Build the scheduler from config.

    ``resource_definitions`` (DB-RESOURCE-007) switches the runtime
    resource source: when provided (bootstrap enabled and applied), each
    provider's resources are built from the sink's ResourceDefinition
    DTOs via the runtime conversion layer, and the YAML
    ``providers.*.resources`` entries are ignored.  ``None`` keeps the
    legacy YAML path unchanged (bootstrap disabled / no-DB deployments).
    """
    registry = ProviderRegistry()
    register_builtin_providers(registry)

    if credential_store is None:
        credential_store = build_credential_store(config)

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
                "(unknown provider id in the 'providers' config section)"
            )
        provider = registry.create(provider_id, pcfg)
        set_credential_store = getattr(provider, "set_credential_store", None)
        if set_credential_store is not None:
            set_credential_store(credential_store)
        if resource_definitions is not None:
            resources = create_runtime_resources(
                registry, resource_definitions, provider_id=provider_id
            )
        else:
            resources = registry.create_resources(
                provider_id, pcfg.get("resources", [])
            )
        for resource in resources:
            if resource.credential_id and resource.credential_id not in credential_store:
                logger.warning(
                    "credential.unresolved provider=%s resource=%s credential_id=%s",
                    provider_id,
                    resource.id,
                    resource.credential_id,
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


def _migrate_legacy_credentials_if_durable(
    config: Dict[str, Any],
    credential_store: CredentialRepository,
    scheduler: Scheduler,
) -> None:
    """AUTH-010: migrate legacy Resource credential fields into the
    repository when the durable backend is active.

    Only the postgres backend migrates: the in-memory backend keeps its
    unchanged AUTH-002 behaviour (legacy fields remain the working
    compatibility path, resources keep ``credential_id=None``).  When
    migration is active, ANY repository failure propagates — startup
    fails rather than continuing with unresolved credentials.
    """
    repo_cfg = config.get("credential_repository") or {}
    if str(repo_cfg.get("backend", "memory")).lower() != "postgres":
        return
    from itertools import chain

    from core.credential_migration import migrate_legacy_resource_credentials

    resources = list(
        chain.from_iterable(pool.resources for pool in scheduler.pools.values())
    )
    migrated = migrate_legacy_resource_credentials(credential_store, resources)
    logger.info(
        "credential.legacy_migrated new=%d resources=%d",
        migrated,
        len(resources),
    )


def _apply_resource_bootstrap(
    definition_repo: Any,
    sink: Any,
    mode: Any,
) -> Any:
    """Apply the resource bootstrap plan and return the sink's final
    definitions plus the structured result.

    Synchronous bridge for the sync ``create_app`` path (ADR-002 §3):
    the repository contract is async, so the apply step drives one
    ``asyncio.run`` here — before ``build_runtime``, because the sink's
    definitions are the runtime build's input (bootstrap apply → runtime
    build, per the frozen startup ordering).  Failures propagate and
    abort startup (fail-closed, ADR-002 §8).
    """

    async def _run() -> Any:
        service = ResourceBootstrapService(
            ResourceRepositoryDefinitionSource(sink),
            sink=sink,
        )
        incoming = await definition_repo.list_definitions()
        result = await service.run(incoming, mode)
        final_definitions = await ResourceRepositoryDefinitionSource(
            sink
        ).list_definitions()
        return result, final_definitions

    return asyncio.run(_run())


def create_app(
    config: Optional[Dict[str, Any]] = None,
    *,
    config_path: Path | str = Path("config.yaml"),
) -> FastAPI:
    if config is None:
        path = Path(config_path)
        cfg = load_config(path) if path.exists() else default_config()
    else:
        cfg = config
    credential_store = build_credential_store(cfg)

    # DB-RESOURCE-006/007: resource bootstrap wiring.  The definition
    # repository (config-seeded) supplies the incoming definitions; the
    # bootstrap service diffs them against the durable sink's current
    # state and applies the plan to the sink.  main.py only composes —
    # it never imports the DTO loader or touches ResourceDefinition
    # parsing.  With bootstrap enabled, the runtime resources are built
    # from the sink's DTOs (not from YAML); disabled keeps the legacy
    # YAML path.  Ordering is the frozen ADR-002 §3 sequence: bootstrap
    # apply → runtime build.
    resource_sink = MemoryResourceRepository()
    definition_repo = create_resource_definition_repository(cfg)
    bootstrap_enabled, bootstrap_mode = resource_bootstrap_settings(cfg)

    bootstrap_result = None
    sink_definitions: Optional[List[Any]] = None
    if bootstrap_enabled:
        bootstrap_result, sink_definitions = _apply_resource_bootstrap(
            definition_repo, resource_sink, bootstrap_mode
        )
        logger.info(
            "resource.bootstrap mode=%s added=%d unchanged=%d "
            "conflicts=%d db_only=%d",
            bootstrap_result.mode.value,
            len(bootstrap_result.added),
            len(bootstrap_result.unchanged),
            len(bootstrap_result.conflicts),
            len(bootstrap_result.db_only),
        )

    scheduler = build_runtime(
        cfg,
        credential_store,
        resource_definitions=sink_definitions,
    )
    _migrate_legacy_credentials_if_durable(cfg, credential_store, scheduler)
    resource_manager = ResourceManager(scheduler, cfg, config_path)

    @asynccontextmanager
    async def lifespan(app_: FastAPI) -> AsyncIterator[None]:
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
    app.state.resource_manager = resource_manager
    app.state.credential_store = credential_store
    app.state.resource_sink = resource_sink
    app.state.resource_definition_repository = definition_repo
    app.state.resource_bootstrap_result = bootstrap_result
    # WEBUI-002 §5: serve the compiled Vue SPA bundle from webui/dist. The
    # mount is applied here (not on the router) so it lands under /admin/, and
    # it is registered *before* the admin router so the SPA catch-all route
    # cannot shadow the hashed asset URLs.
    mount_admin_assets(app)
    app.include_router(models_router)
    app.include_router(chat_router)
    app.include_router(admin_router)

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
