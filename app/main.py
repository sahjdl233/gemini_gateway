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

from fastapi import Depends, FastAPI

from app.bootstrap import register_builtin_providers
from app.routes.api_auth import (
    build_api_auth_settings,
    client_auth_error_response,
    require_client_auth,
    ClientAuthError,
)
from app.routes.chat import router as chat_router
from app.routes.models import router as models_router
from app.routes.admin import mount_admin_assets
from app.routes.admin import router as admin_router
from app.management import ResourceManager
from config.loader import default_config, load_config
from core.cooldown import CooldownManager
from core.resource_bootstrap import BootstrapMode, ResourceBootstrapService
from core.resource_definition_repository import (
    ResourceRepositoryDefinitionSource,
)
from core.resource_definition_repository_factory import (
    create_config_definition_source,
    create_resource_definition_repository,
    resource_bootstrap_settings,
)
from core.resource_repository_factory import (
    create_resource_repository,
    resource_store_backend,
)
from core.runtime_reconciliation import RuntimeReconciliationService
from core.runtime_resource_factory import (
    create_config_resource_source,
    registry_runtime_builder,
)
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
    registry: Optional[ProviderRegistry] = None,
    resource_snapshot: Optional[Any] = None,
) -> Scheduler:
    """Build the scheduler from config.

    Resource source selection (DB-RESOURCE-007/008):

    * ``resource_snapshot`` (a :class:`core.runtime_snapshot.RuntimeSnapshot`
      produced by ``RuntimeReconciliationService``) — resources come from
      the reconciled definitions; YAML ``providers.*.resources`` entries
      are ignored.  Used when bootstrap is enabled.
    * ``None`` — legacy YAML path via the config resource source
      (bootstrap disabled / no-DB deployments).

    Resource creation itself lives in ``core.runtime_resource_factory``
    (config source) and ``core.runtime_reconciliation`` (snapshot path);
    this function only wires pools and the scheduler.
    """
    if registry is None:
        registry = ProviderRegistry()
        register_builtin_providers(registry)

    if credential_store is None:
        credential_store = build_credential_store(config)

    pools: Dict[str, Any] = {}
    providers: Dict[str, Any] = {}
    cooldown_cfg = config.get("scheduler", {}).get("cooldown", {})
    cooldown = CooldownManager(**cooldown_cfg)
    resource_source = create_config_resource_source(registry)

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
        if resource_snapshot is not None:
            resources = list(
                resource_snapshot.resources_by_provider.get(provider_id, [])
            )
        else:
            resources = resource_source(
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


async def _initialize_persistence_repositories(
    resource_sink: Any,
    definition_repo: Any,
) -> None:
    """Idempotent R6/R7 persistence initialization (ANON-012 Part B).

    The legacy sink's own ``initialize()`` only creates the BASE
    ``resource_definitions`` table — the full R6 migration (id /
    created_at / updated_at upgrade columns, ``credentials`` and
    ``runtime_state``) lives behind
    :meth:`PostgresResourceDefinitionRepository.initialize` →
    ``initialize_persistence()`` and MUST run at startup, independent of
    whether resource bootstrap is enabled.  Both repositories target the
    same database/tables and every statement is idempotent, so R7's
    dual-side layout (legacy write sink + new-protocol source of record)
    initializes correctly in any order.
    """
    definition_initialize = getattr(definition_repo, "initialize", None)
    if definition_initialize is not None:
        await definition_initialize()
    sink_initialize = getattr(resource_sink, "initialize", None)
    if sink_initialize is not None:
        await sink_initialize()


def _apply_resource_bootstrap(
    definition_repo: Any,
    sink: Any,
    mode: Any,
    registry: ProviderRegistry,
    *,
    reconciliation_repository: Any = None,
) -> Any:
    """Apply the resource bootstrap plan, then reconcile the runtime from
    the sink; return ``(bootstrap_result, runtime_snapshot)``.

    Synchronous bridge for the sync ``create_app`` path (ADR-002 §3):
    the repository contracts are async, so the apply + reconcile steps
    drive one ``asyncio.run`` here — before ``build_runtime``, because
    the snapshot is the runtime build's input (bootstrap apply → runtime
    reconciliation → runtime build, per the frozen startup ordering).
    Failures propagate and abort startup (fail-closed, ADR-002 §8).
    """

    async def _run() -> Any:
        # Schema init first (ADR-002 §3: repository init → bootstrap):
        # repeatable CREATE TABLE IF NOT EXISTS on the durable store;
        # the memory sink has no initialize.
        initialize = getattr(sink, "initialize", None)
        if initialize is not None:
            await initialize()
        service = ResourceBootstrapService(
            ResourceRepositoryDefinitionSource(sink),
            sink=sink,
        )
        incoming = await definition_repo.list_definitions()
        result = await service.run(incoming, mode)
        # CONFIG/R-7: the reconciliation source is the persistence-layer
        # ResourceDefinitionRepository (postgres) when provided; memory
        # deployments keep the legacy sink-backed read source.  Bootstrap
        # diff/apply above stays on the write-side repository.
        reconciliation = RuntimeReconciliationService(
            reconciliation_repository
            if reconciliation_repository is not None
            else ResourceRepositoryDefinitionSource(sink),
            runtime_builder=registry_runtime_builder(registry),
        )
        snapshot = await reconciliation.reconcile()
        return result, snapshot

    return asyncio.run(_run())


def _log_bootstrap_outcome(bootstrap_result: Any, runtime_snapshot: Any) -> None:
    """Emit the bootstrap outcome at the right severities (CONFIG-001 P2/P4).

    Summary counts stay INFO.  Per-key conflicts and db_only records are
    WARNING with a secret-free canonical diff — a stale seed facing a
    newer repository must be impossible to miss, because the drift is the
    input to every future import decision
    (docs/CONFIG-001-BOOTSTRAP-SOURCE-OF-TRUTH.md §4).
    """
    logger.info(
        "resource.bootstrap mode=%s added=%d unchanged=%d conflicts=%d "
        "db_only=%d",
        bootstrap_result.mode.value,
        len(bootstrap_result.added),
        len(bootstrap_result.unchanged),
        len(bootstrap_result.conflicts),
        len(bootstrap_result.db_only),
    )
    for conflict in bootstrap_result.conflicts:
        logger.warning(
            "bootstrap resource conflict: provider=%s resource_id=%s "
            "existing != incoming, keeping repository value; diff=%s",
            conflict.provider,
            conflict.resource_id,
            conflict.diff(),
        )
    for record in bootstrap_result.db_only:
        logger.warning(
            "bootstrap db_only resource: provider=%s resource_id=%s "
            "database contains resources absent from bootstrap seed; "
            "resources preserved",
            record.provider,
            record.resource_id,
        )
    if (
        bootstrap_result.mode is BootstrapMode.CHECK
        and runtime_snapshot.source_count == 0
        and bootstrap_result.added
    ):
        logger.warning(
            "bootstrap check mode did not import resources: repository is "
            "empty and runtime will start without resources (seed offers "
            "%d). Use import mode for initial deployment.",
            len(bootstrap_result.added),
        )


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

    # DB-RESOURCE-006/007/008: resource bootstrap + reconciliation
    # wiring.  The definition repository (config-seeded) supplies the
    # incoming definitions; the bootstrap service diffs them against the
    # durable sink's current state and applies the plan to the sink; the
    # reconciliation service turns the sink's definitions into a runtime
    # snapshot.  main.py only composes — it never imports the DTO loader,
    # never converts definitions and never creates resources itself.
    # Disabled bootstrap keeps the legacy YAML path.  Ordering is the
    # frozen ADR-002 §3 sequence: bootstrap apply → runtime
    # reconciliation → runtime build.
    # DB-RESOURCE-011: the sink backend is now selectable —
    # resource_store.backend: memory (default, no DB required) | postgres
    # (DSN via GEMINI_GATEWAY_DATABASE_URL, fail-closed when missing).
    # The bootstrap incoming seed stays config-backed in both cases
    # (YAML → store import semantics, ADR-002 §2); the definition source
    # of record is the durable store when postgres.
    resource_sink = create_resource_repository(cfg)
    incoming_source = create_config_definition_source(cfg)
    definition_repo = create_resource_definition_repository(cfg)
    bootstrap_enabled, bootstrap_mode = resource_bootstrap_settings(cfg)

    # ANON-012 Part B: when the durable store is postgres, startup ALWAYS
    # completes the full idempotent R6 migration (resource_definitions
    # with id/created_at/updated_at, credentials, runtime_state) — not
    # just the legacy sink's base table, and independent of whether
    # bootstrap is enabled.  Failures propagate and abort startup
    # (fail-closed, same contract as bootstrap).
    if resource_store_backend(cfg) == "postgres":
        asyncio.run(
            _initialize_persistence_repositories(resource_sink, definition_repo)
        )

    registry = ProviderRegistry()
    register_builtin_providers(registry)

    bootstrap_result = None
    runtime_snapshot = None
    if bootstrap_enabled:
        # CONFIG/R-7: postgres production wiring hands the reconciliation
        # service the new-protocol repository (the source of record);
        # memory deployments reconcile from their imported sink.
        reconciliation_repository = (
            definition_repo
            if resource_store_backend(cfg) == "postgres"
            else None
        )
        bootstrap_result, runtime_snapshot = _apply_resource_bootstrap(
            incoming_source,
            resource_sink,
            bootstrap_mode,
            registry,
            reconciliation_repository=reconciliation_repository,
        )
        _log_bootstrap_outcome(bootstrap_result, runtime_snapshot)
        logger.info(
            "runtime.reconciled source_count=%d resources=%d",
            runtime_snapshot.source_count,
            len(runtime_snapshot.resources),
        )

    scheduler = build_runtime(
        cfg,
        credential_store,
        registry=registry,
        resource_snapshot=runtime_snapshot,
    )
    _migrate_legacy_credentials_if_durable(cfg, credential_store, scheduler)
    # DB-RESOURCE-013: when the runtime source is the repository
    # (bootstrap enabled), Admin resource mutations write through the
    # repository and reconcile; otherwise the legacy YAML path is kept.
    resource_manager = ResourceManager(
        scheduler,
        cfg,
        config_path,
        repository=resource_sink if bootstrap_enabled else None,
        runtime_builder=registry_runtime_builder(registry),
        credential_store=credential_store,
        # The real scheduler always exposes its registry; getattr keeps
        # minimal scheduler stand-ins (tests) working — the manager's
        # dependency is optional by contract (CONFIG/R-2-C).
        model_registry=getattr(scheduler, "model_registry", None),
    )

    @asynccontextmanager
    async def lifespan(app_: FastAPI) -> AsyncIterator[None]:
        try:
            # ANON-012: providers may own background lifecycles (e.g. the
            # anonymous_vertex admission scheduler).  Start them AFTER the
            # runtime is built and BEFORE the first request, via an
            # optional hook — generic getattr, no provider-specific import.
            for provider in scheduler.providers.values():
                start = getattr(provider, "start_admission", None)
                if start is not None:
                    await start()
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
    # GATEWAY-002: the /v1 client-auth seam.  Settings are parsed once at
    # startup (fail-closed on misconfiguration); the dependency reads the
    # snapshot per request and rejects unauthenticated calls BEFORE the
    # route handlers touch the scheduler.  /admin keeps its own
    # ADMIN_TOKEN boundary — the two are independent.
    app.state.api_auth = build_api_auth_settings(cfg)
    app.add_exception_handler(ClientAuthError, client_auth_error_response)
    v1_dependencies = [Depends(require_client_auth)]
    app.state.scheduler = scheduler
    app.state.config = cfg
    app.state.resource_manager = resource_manager
    app.state.credential_store = credential_store
    app.state.resource_sink = resource_sink
    app.state.resource_definition_repository = definition_repo
    app.state.resource_bootstrap_result = bootstrap_result
    app.state.runtime_snapshot = runtime_snapshot
    # WEBUI-002 §5: serve the compiled Vue SPA bundle from webui/dist. The
    # mount is applied here (not on the router) so it lands under /admin/, and
    # it is registered *before* the admin router so the SPA catch-all route
    # cannot shadow the hashed asset URLs.
    mount_admin_assets(app)
    app.include_router(models_router, dependencies=v1_dependencies)
    app.include_router(chat_router, dependencies=v1_dependencies)
    app.include_router(admin_router)  # /admin: ADMIN_TOKEN, unchanged

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
