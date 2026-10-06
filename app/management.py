"""Runtime management for the existing Antigravity ``InMemoryPool``.

This module deliberately keeps management state inside the existing Resource
objects and pool. It is not a second resource model or scheduler.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import yaml

from core.health import HealthState
from core.pool import InMemoryPool
from core.resource_definition import (
    PROVIDER_DEFINITION_TYPES,
    ResourceDefinitionBase,
    parse_resource_definition,
)
from core.resource_definition_repository import (
    ResourceRepositoryDefinitionSource,
)
from core.resource_repository import (
    DuplicateResourceDefinitionError,
    UnknownResourceDefinitionError,
)
from core.runtime_reconciliation import RuntimeReconciliationService
from providers.antigravity.resource import AntigravityResource

logger = logging.getLogger(__name__)


# WEBUI-001: the Admin Resource surface manages *runtime* configuration
# only.  Long-lived credential material is owned by CredentialRepository and
# reached through ``credential_id``; the legacy provider-specific secret
# fields below stay on the Resource (startup compatibility, AUTH-010) but are
# never an Admin WebUI / Resource API write target.
_CREATE_FIELDS = (
    "id",
    "provider",
)
_EDITABLE_FIELDS = (
    "enabled",
    "credential_id",
    "project_id",
    "ide_type",
)

# Secret-like fields that must be rejected by the Admin Resource API.  The
# rejection message never echoes the submitted value.
_SECRET_FIELDS = (
    "access_token",
    "refresh_token",
    "client_id",
    "client_secret",
    "api_key",
    "debug_token",
)

# Legacy provider-specific credential fields: startup compatibility input and
# existing config.yaml content only (AUTH-010).  They are preserved verbatim
# on persist and are never writable through the Admin API.
_LEGACY_CREDENTIAL_FIELDS = (
    "access_token",
    "refresh_token",
    "client_id",
    "client_secret",
)

_PERSISTED_FIELDS = (
    _CREATE_FIELDS + _EDITABLE_FIELDS + ("token_expiry",) + _LEGACY_CREDENTIAL_FIELDS
)


class ResourceManagementError(Exception):
    """A safe management error whose message never contains credentials."""


class ResourceManager:
    """Manage the live Antigravity resources and the existing YAML config.

    Write path selection (DB-RESOURCE-013):

    * ``repository`` provided (bootstrap enabled — the runtime source IS
      the repository): mutations go  repository.add/update/delete →
      runtime reconciliation → pools ; YAML is never written.  Works
      with the memory sink and the PostgreSQL store alike.
    * ``repository is None`` (bootstrap disabled): the legacy
      Pool-first + YAML-persist path is kept — YAML remains that
      deployment's store (Part C compatibility).
    """

    def __init__(
        self,
        scheduler: Any,
        config: Dict[str, Any],
        config_path: Path | str = Path("config.yaml"),
        *,
        repository: Any = None,
        runtime_builder: Any = None,
        credential_store: Any = None,
        model_registry: Any = None,
    ) -> None:
        self.scheduler = scheduler
        self.config = config
        self.config_path = Path(config_path)
        self._repository = repository
        self._runtime_builder = runtime_builder
        #: AUTH-016: the credential store backs resource-write-time
        #: credential existence validation (a bound credential_id must
        #: resolve at mutation time — strict reference integrity carried
        #: from the request path into the control plane).
        self._credential_store = credential_store
        #: CONFIG/R-2-C: optional discovery-cache hook.  After a
        #: definition mutation reaches the runtime (reconcile success on
        #: the repository path, pool+persist success on the legacy
        #: path), the model index is invalidated so the next discovery
        #: reflects the new definitions.  Optional for backward
        #: compatibility; the registry contract is
        #: docs/CONFIG-R2B-MODEL-REGISTRY-INVALIDATION.md.
        self._model_registry = model_registry
        #: Latest reconciliation snapshot (updated on every repository
        #: write); consumable by admin/health features without touching
        #: the scheduler.
        self.last_snapshot = None
        pools = getattr(scheduler, "pools", {})
        self.pool: InMemoryPool | None = pools.get("antigravity")
        self._lock = asyncio.Lock()
        #: CONTROL-005-FIX: serializes the full repository mutation
        #: lifecycle (read current → repository write → reconcile →
        #: rollback/compensation → return).  The legacy ``_lock`` above
        #: only guards the YAML/Pool-first path; repository writes have
        #: their own lock so the two write paths never interact.  A
        #: single-process asyncio lock: multi-worker / multi-instance
        #: deployments remain a future concern (distributed lock or
        #: version CAS), deliberately out of scope here.
        self._repository_lock = asyncio.Lock()
        self._source_config = self._read_source()
        self._original_values: Dict[str, Dict[str, Any]] = {
            resource.id: self._resource_values(resource)
            for resource in self._resources()
        }

    @property
    def _repository_backed(self) -> bool:
        return self._repository is not None

    @property
    def _durable_credentials(self) -> bool:
        """True when the durable CredentialRepository backend is active.

        WEBUI-001: in postgres mode the Credential owns the long-lived
        material, so Resource management must never (re)write it into
        ``config.yaml``.  Existing legacy fields already in the file are left
        untouched on purpose — this task does not widen the AUTH-010
        migration scope.
        """
        repo_cfg = self.config.get("credential_repository") or {}
        return str(repo_cfg.get("backend", "memory")).lower() == "postgres"

    def _read_source(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            return copy.deepcopy(self.config)
        try:
            raw = self.config_path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ResourceManagementError(
                "resource configuration cannot be read"
            ) from exc
        return data if isinstance(data, dict) else {}

    def _resources(self) -> Iterable[AntigravityResource]:
        if self.pool is None:
            return ()
        return (
            resource
            for resource in self.pool.resources
            if isinstance(resource, AntigravityResource)
        )

    @staticmethod
    def _resource_values(resource: AntigravityResource) -> Dict[str, Any]:
        return {
            field: getattr(resource, field, None)
            for field in _PERSISTED_FIELDS
        }

    def _require_pool(self) -> InMemoryPool:
        if self.pool is None:
            raise ResourceManagementError(
                "Antigravity provider is not configured"
            )
        return self.pool

    # -- DB-RESOURCE-014: provider-aware resource management -------------

    #: Resource fields that are runtime/scheduling state and never part
    #: of a persisted definition payload.
    _RUNTIME_ONLY_FIELDS = frozenset(
        {
            "health",
            "cooldown_until",
            "in_flight",
            "total_requests",
            "total_failures",
            "consecutive_failures",
            "resource_key",
        }
    )
    _COMMON_DEFINITION_FIELDS = frozenset(
        {"id", "provider", "enabled", "credential_id"}
    )

    def _pool_for(self, provider_id: str) -> InMemoryPool:
        pool = getattr(self.scheduler, "pools", {}).get(provider_id)
        if pool is None:
            raise ResourceManagementError(
                f"unknown or unmanaged provider: {provider_id!r}"
            )
        return pool

    def _legacy_provider_guard(self, provider_id: str) -> None:
        """The YAML-persist path predates provider awareness: it manages
        the Antigravity pool only (deprecated compatibility)."""
        if provider_id != "antigravity":
            raise ResourceManagementError(
                f"provider {provider_id!r} requires resource bootstrap "
                "to be enabled"
            )

    def get_resource(self, provider_id: str, resource_id: str) -> Any:
        """Runtime instance lookup (pool view) — used by the write path
        after reconciliation.  The READ path is
        :meth:`get_definition_view` / :meth:`list_resources`."""
        pool = self._pool_for(provider_id)
        for resource in pool.resources:
            if resource.id == resource_id:
                return resource
        raise ResourceManagementError("resource not found")

    # -- DB-RESOURCE-014/CONTROL-002: definition-sourced read path -------

    def _definition_source(self) -> Any:
        return ResourceRepositoryDefinitionSource(self._repository)

    def _runtime_lookup(
        self, provider_id: str, resource_id: str
    ) -> Optional[Any]:
        """Best-effort live Resource for runtime observability fields;
        absence never hides a definition (Part A)."""
        pool = getattr(self.scheduler, "pools", {}).get(provider_id)
        if pool is None:
            return None
        for resource in pool.resources:
            if resource.id == resource_id:
                return resource
        return None

    def serialize_definition(
        self, definition: ResourceDefinitionBase, runtime_resource: Any = None
    ) -> Dict[str, Any]:
        """Definition state → response item (CONTROL-002 Part C).

        Definition fields come from the repository DTO only; the
        runtime observability block (health / counters) is merged
        best-effort from the live pool and is therefore fully
        independent of the definition values.  Secrets cannot enter:
        the DTO layer rejects secret fields by construction and the
        body is the provider's allowlisted definition body.
        """
        item = definition.to_runtime_definition()
        if runtime_resource is not None:
            item["health"] = (
                runtime_resource.health.value
                if hasattr(runtime_resource.health, "value")
                else str(runtime_resource.health)
            )
            item["cooldown_until"] = (
                runtime_resource.cooldown_until.isoformat()
                if runtime_resource.cooldown_until is not None
                else None
            )
            item["in_flight"] = runtime_resource.in_flight
            item["total_requests"] = runtime_resource.total_requests
            item["total_failures"] = runtime_resource.total_failures
        else:
            item["health"] = None
            item["cooldown_until"] = None
            item["in_flight"] = None
            item["total_requests"] = None
            item["total_failures"] = None
        return item

    async def list_resources(
        self, provider_id: Optional[str] = None
    ) -> list[Dict[str, Any]]:
        """Definition-sourced listing (CONTROL-002 Part A).  Reads the
        repository — never the pools — so a resource exists because the
        store says so, not because a pool was built."""
        if not self._repository_backed:
            # Legacy deployments: YAML is the store, the pool is the view.
            if provider_id is not None:
                self._legacy_provider_guard(provider_id)
            return [self.serialize(r) for r in self._resources()]
        definitions = await self._definition_source().list_definitions()
        items = [
            self.serialize_definition(
                definition,
                self._runtime_lookup(definition.provider, definition.id),
            )
            for definition in definitions
        ]
        if provider_id is not None:
            items = [i for i in items if i["provider"] == provider_id]
        return items

    async def get_definition_view(
        self, provider_id: str, resource_id: str
    ) -> Dict[str, Any]:
        """Scoped read (CONTROL-002 Part B): repository-sourced, with the
        legacy antigravity pool view for bootstrap-disabled deployments.

        provider 未管理（unknown to the DTO layer, or unmanaged on the
        legacy path）→ 400; known provider + missing resource → 404.
        """
        if not self._repository_backed:
            self._legacy_provider_guard(provider_id)
            return self.serialize(self.get_resource(provider_id, resource_id))
        if provider_id not in PROVIDER_DEFINITION_TYPES:
            raise ResourceManagementError(
                f"unknown or unmanaged provider: {provider_id!r}"
            )
        definition = await self._definition_source().get_definition(
            provider_id, resource_id
        )
        if definition is None:
            raise ResourceManagementError("resource not found")
        return self.serialize_definition(
            definition, self._runtime_lookup(provider_id, resource_id)
        )


    def serialize(self, resource: Any) -> dict[str, Any]:
        """Generic runtime-resource serialization: common scheduling
        fields plus the provider-specific config fields (minus legacy
        credential material, which is never exposed)."""
        health = (
            resource.health.value
            if hasattr(resource.health, "value")
            else str(resource.health)
        )
        cooldown = (
            resource.cooldown_until.isoformat()
            if resource.cooldown_until is not None
            else None
        )
        excluded = (
            self._RUNTIME_ONLY_FIELDS
            | self._COMMON_DEFINITION_FIELDS
            | set(_LEGACY_CREDENTIAL_FIELDS)
            | {"token_expiry"}
        )
        body = {
            key: value
            for key, value in resource.model_dump().items()
            if key not in excluded
        }
        return {
            "id": resource.id,
            "provider": resource.provider,
            "enabled": resource.enabled,
            "credential_id": resource.credential_id,
            "health": health,
            "cooldown_until": cooldown,
            "in_flight": resource.in_flight,
            "total_requests": resource.total_requests,
            "total_failures": resource.total_failures,
            **body,
        }

    @staticmethod
    def _reject_secret_fields(payload: Dict[str, Any]) -> None:
        """Refuse credential material in an Admin Resource mutation.

        WEBUI-001: ``Resource`` is the runtime/scheduling object; durable
        credential material belongs to ``Credential`` and is reached through
        ``credential_id``.  The rejected *field names* are safe to report,
        the submitted *values* never are.
        """
        offending = sorted(
            field for field in _SECRET_FIELDS if field in payload
        )
        if offending:
            raise ResourceManagementError(
                "resource credential fields are managed through /admin/credentials: "
                + ", ".join(offending)
            )

    @staticmethod
    def _validate_values(values: Dict[str, Any], *, creating: bool) -> None:
        if creating:
            resource_id = values.get("id")
            if not isinstance(resource_id, str) or not resource_id.strip():
                raise ResourceManagementError("resource id is required")

        for field in _CREATE_FIELDS:
            if field == "id":
                continue
            value = values.get(field)
            if value is not None and not isinstance(value, str):
                raise ResourceManagementError(f"{field} must be a string")
        if "enabled" in values and not isinstance(values["enabled"], bool):
            raise ResourceManagementError("enabled must be a boolean")

        for field in _EDITABLE_FIELDS:
            if field == "enabled":
                continue
            value = values.get(field)
            if value is not None and not isinstance(value, str):
                raise ResourceManagementError(f"{field} must be a string")

    @property
    def _repository_backed(self) -> bool:
        return self._repository is not None

    @asynccontextmanager
    async def mutation_lock(self):
        """Serialize a control-plane mutation against resource writes.

        AUTH-016 B: credential DELETE/PATCH must not interleave with
        resource mutations — otherwise a credential could be removed
        between a resource write's existence check and its store write,
        or a concurrent bind could resurrect a reference the delete just
        verified absent.  Both write paths are covered: repository
        mutations hold ``_repository_lock`` (CONTROL-005-FIX), legacy
        mutations hold ``_lock``; credential mutations take both, in a
        fixed order (no cycle → no deadlock).  Exposed for the Admin
        credential routes.
        """
        async with self._repository_lock:
            async with self._lock:
                yield

    def _validate_credential_reference(self, payload: Dict[str, Any]) -> None:
        """AUTH-016 B: a set ``credential_id`` must resolve at write time.

        Carries the request-path strict reference integrity (AUTH-013)
        into the control plane: an Admin resource mutation may never
        create the ``enabled=true, credential_id missing`` half-broken
        state that only surfaces as request-time 401s.  Bootstrap import
        keeps its documented warn-only semantics (ADR-002 §5) — this is
        the Admin write boundary, not the import path.
        """
        credential_id = payload.get("credential_id")
        if credential_id is None:
            return
        if self._credential_store is None:
            raise ResourceManagementError(
                "credential store is unavailable; cannot validate "
                f"credential_id {credential_id!r}"
            )
        if self._credential_store.get(credential_id) is None:
            raise ResourceManagementError(
                f"credential {credential_id!r} not found"
            )

    # -- DB-RESOURCE-013 / CONTROL-004-FIX: repository write path --------

    async def _invalidate_provider_adapters(
        self, provider_id: str, resource_ids: Iterable[str]
    ) -> None:
        """Notify a provider that per-resource auth state must be dropped.

        CONTROL-006-FIX-1: the provider's auth-adapter cache is keyed by
        ``resource.id`` and survives resource replacement/removal — a
        re-bound or deleted-and-recreated resource would inherit the old
        OAuth lifecycle (rotated refresh token included).  The capability
        is optional: providers without adapter caches simply don't
        implement it (duck-typed, like ``set_discovery_resource_source``).
        """
        provider = getattr(self.scheduler, "providers", {}).get(provider_id)
        invalidate = getattr(provider, "invalidate_resource", None)
        if invalidate is None:
            return
        from core.provider import await_if_needed

        for resource_id in resource_ids:
            # sync or async provider seam (firebase closes owned transports,
            # so its invalidation is async); both are supported here
            await await_if_needed(invalidate(resource_id))

    def _stale_adapter_ids(
        self, old_resources: Iterable[Any], new_resources: List[Any]
    ) -> set:
        """Resource ids whose definition was replaced or removed.

        A resource qualifies when it disappeared from the new snapshot
        (delete) or its definition payload changed (credential rebind,
        disable, any editable field) — i.e. exactly the cases where the
        old adapter's OAuth lifecycle state must not outlive the
        definition it was created for.
        """
        new_by_id = {resource.id: resource for resource in new_resources}
        return {
            old.id
            for old in old_resources
            if old.id not in new_by_id
            or self._definition_payload_from_resource(old)
            != self._definition_payload_from_resource(new_by_id[old.id])
        }

    async def _reconcile_runtime(self) -> None:
        """Rebuild runtime resources from the repository and apply them
        to every pool.  Runtime state is preserved per ResourceKey by
        ``pool.reconcile_resources``; in-flight violations propagate as
        management errors.

        NOTE (CONTROL-004 audit W3, accepted): the per-pool loop is not
        atomic — a failure in a later pool leaves earlier pools updated.
        The store is the source of truth; state converges on the next
        successful write or restart."""
        service = RuntimeReconciliationService(
            ResourceRepositoryDefinitionSource(self._repository),
            runtime_builder=self._runtime_builder,
        )
        snapshot = await service.reconcile()
        self.last_snapshot = snapshot
        # CONTROL-006-FIX-1: capture which resource definitions were
        # replaced or removed BEFORE the pools are applied (reconcile
        # mutates them) — their provider auth-adapter OAuth state must
        # not outlive the definition it was created for.
        stale_by_provider = {
            provider_id: self._stale_adapter_ids(
                pool.resources,
                snapshot.resources_by_provider.get(provider_id, []),
            )
            for provider_id, pool in self.scheduler.pools.items()
        }
        try:
            for provider_id, pool in self.scheduler.pools.items():
                await pool.reconcile_resources(
                    snapshot.resources_by_provider.get(provider_id, [])
                )
        except RuntimeError as exc:
            raise ResourceManagementError(str(exc)) from exc
        # The pool application succeeded, so the replacement/removal is
        # committed — only now drop the adapters (a failed reconcile is
        # rolled back and must leave the providers untouched).
        for provider_id, stale in stale_by_provider.items():
            if stale:
                await self._invalidate_provider_adapters(provider_id, stale)
        # CONFIG/R-2-C: the definitions are live in the runtime — the
        # discovery cache must not keep serving the old index.  Only
        # this SUCCESS point invalidates; a reconcile failure is rolled
        # back above and leaves the cache untouched.
        self._invalidate_model_registry()

    def _invalidate_model_registry(self) -> None:
        """Notify the optional ModelRegistry that definitions changed.

        CONFIG/R-2-C: called strictly AFTER a definition mutation has
        taken effect in the runtime.  Registry contract:
        docs/CONFIG-R2B-MODEL-REGISTRY-INVALIDATION.md — invalidate is
        lazy, state-preserving and never called for runtime scheduling
        state.  No-op when no registry was provided."""
        if self._model_registry is not None:
            self._model_registry.invalidate()

    @staticmethod
    def _handle_compensation_failure(
        reconcile_exc: BaseException, rollback_exc: Exception
    ) -> None:
        """A rollback/compensation step failed after a reconcile failure.

        Never swallowed, never silent: both failures are logged and the
        raised error carries BOTH — the original reconcile failure in
        the message (and as ``__context__`` /
        ``.original_reconcile_error``), the compensation failure as
        ``__cause__``.  ``BaseException`` originals (cancellation) are
        re-raised unwrapped."""
        logger.error(
            "resource.write compensation failed: original=%s: %s | "
            "rollback=%s: %s",
            type(reconcile_exc).__name__,
            reconcile_exc,
            type(rollback_exc).__name__,
            rollback_exc,
        )
        if not isinstance(reconcile_exc, Exception):
            raise reconcile_exc
        error = ResourceManagementError(
            f"{reconcile_exc}; compensation failed: {rollback_exc}"
        )
        error.original_reconcile_error = reconcile_exc
        raise error from rollback_exc

    async def _rollback_update(
        self,
        provider_id: str,
        resource_id: str,
        previous: Optional[ResourceDefinitionBase],
        reconcile_exc: BaseException,
    ) -> None:
        """Restore the pre-update store state: the previous definition
        (full replacement) or, when none existed, no row at all."""
        try:
            if previous is not None:
                await self._repository.update(previous)
            else:
                await self._repository.delete(provider_id, resource_id)
        except Exception as rollback_exc:
            self._attach_rollback_failure(reconcile_exc, rollback_exc)

    def _definition_payload_from_resource(self, resource: Any) -> Dict[str, Any]:
        """Runtime resource → definition payload (generic, any provider):
        everything except runtime state; the strict DTO parse decides
        which fields the provider's definition actually allows."""
        return {
            key: value
            for key, value in resource.model_dump().items()
            if key not in self._RUNTIME_ONLY_FIELDS
        }

    def _parse_definition(
        self, payload: Dict[str, Any]
    ) -> ResourceDefinitionBase:
        try:
            return parse_resource_definition(payload)
        except ValueError as exc:
            # pydantic ValidationError / ResourceDefinitionError: strict
            # DTO validation is the write boundary — never stripped down
            # to make a payload pass.
            raise ResourceManagementError(
                f"invalid resource definition: {exc}"
            ) from exc

    async def _repository_create(
        self, payload: Dict[str, Any], provider_id: str
    ) -> Any:
        # CONTROL-005-FIX: the lock spans the whole mutation lifecycle —
        # validation, store write, reconcile and compensation — so a
        # concurrent write can never interleave between them.
        async with self._repository_lock:
            values = dict(payload)
            values["provider"] = provider_id
            definition = self._parse_definition(values)
            self._validate_credential_reference(values)
            try:
                await self._repository.add(definition)
            except DuplicateResourceDefinitionError:
                raise ResourceManagementError(
                    "resource already exists"
                ) from None
            try:
                await self._reconcile_runtime()
            except BaseException as reconcile_exc:
                # Compensate: the store write is rolled back so the store and
                # the runtime stay consistent with each other.  A failing
                # compensation never masks the original reconcile error —
                # it is logged and chained as __cause__ (CONTROL-004-FIX 3).
                try:
                    await self._repository.delete(
                        definition.provider, definition.id
                    )
                except Exception as rollback_exc:
                    self._handle_compensation_failure(
                        reconcile_exc, rollback_exc
                    )
                raise
            return self.get_resource(provider_id, definition.id)

    async def _repository_update(
        self, provider_id: str, resource_id: str, payload: Dict[str, Any]
    ) -> Any:
        # CONTROL-005-FIX: full lifecycle under the lock — without it a
        # concurrent writer could commit between this request's store
        # write and its reconcile (runtime stale), or its failure
        # rollback could replace a newer committed definition with the
        # stale one captured below (rollback clobber).
        async with self._repository_lock:
            current = await self._repository.get(provider_id, resource_id)
            if current is None:
                # Defensive: runtime resource exists but no stored definition
                # (should not happen on the repository path) — derive one
                # from the live resource so the write is still full-replacement.
                resource = self.get_resource(provider_id, resource_id)
                base = self._definition_payload_from_resource(resource)
            else:
                base = current.to_runtime_definition()
            payload_resource_id = payload.get("id")
            if (
                payload_resource_id is not None
                and payload_resource_id != resource_id
            ):
                raise ResourceManagementError("resource id cannot be changed")
            base.update(payload)
            base["provider"] = provider_id
            base["id"] = resource_id
            definition = self._parse_definition(base)
            self._validate_credential_reference(base)
            try:
                await self._repository.update(definition)
            except UnknownResourceDefinitionError:
                raise ResourceManagementError(
                    "resource not found"
                ) from None
            try:
                await self._reconcile_runtime()
            except BaseException as reconcile_exc:
                # CONTROL-004-FIX 1: restore the pre-update store state so DB
                # and runtime stay consistent; the original reconcile error
                # keeps its semantics and propagates.
                try:
                    await self._rollback_update(
                        provider_id, resource_id, current, reconcile_exc
                    )
                except Exception as rollback_exc:
                    self._handle_compensation_failure(
                        reconcile_exc, rollback_exc
                    )
                raise
            return self.get_resource(provider_id, resource_id)

    async def _repository_delete(
        self, provider_id: str, resource_id: str
    ) -> None:
        # CONTROL-005-FIX: full lifecycle under the lock — otherwise a
        # concurrent update committing between the existence checks and
        # the delete would be silently destroyed by this delete.
        async with self._repository_lock:
            # Existence check against the live pool for the friendly 404; the
            # repository delete itself is idempotent.
            self.get_resource(provider_id, resource_id)
            previous = await self._repository.get(provider_id, resource_id)
            await self._repository.delete(provider_id, resource_id)
            try:
                await self._reconcile_runtime()
            except BaseException as reconcile_exc:
                # CONTROL-004-FIX 2: restore the deleted definition (all
                # fields) so the runtime does not become a ghost; the
                # original reconcile error keeps its semantics.
                if previous is not None:
                    try:
                        await self._repository.add(previous)
                    except Exception as rollback_exc:
                        self._handle_compensation_failure(
                            reconcile_exc, rollback_exc
                        )
                raise

    async def create_resource(
        self, payload: Dict[str, Any], *, provider_id: str
    ) -> Any:
        """Create a resource definition for ``provider_id``.

        ``provider_id`` must name a configured provider (Part C); the
        payload is validated by the strict DTO boundary, which rejects
        unknown providers and fields alike.
        """
        self._reject_secret_fields(payload)
        if self._repository_backed:
            if "id" not in payload:
                raise ResourceManagementError("resource id is required")
            return await self._repository_create(payload, provider_id)
        self._legacy_provider_guard(provider_id)
        return await self._legacy_create(payload)

    async def update_resource(
        self, provider_id: str, resource_id: str, payload: Dict[str, Any]
    ) -> Any:
        if self._repository_backed:
            # Field allowlisting is the strict DTO parse (extra="forbid",
            # per-provider) — no manager-level field list, so every
            # provider's editable fields come from its own definition.
            self._reject_secret_fields(payload)
            return await self._repository_update(
                provider_id, resource_id, payload
            )
        self._legacy_provider_guard(provider_id)
        self._reject_secret_fields(payload)
        unknown = set(payload) - set(_EDITABLE_FIELDS)
        if unknown:
            raise ResourceManagementError("resource contains unsupported fields")
        self._validate_values(payload, creating=False)
        return await self._legacy_update(resource_id, payload)

    async def set_enabled(
        self, provider_id: str, resource_id: str, enabled: bool
    ) -> Any:
        return await self.update_resource(
            provider_id, resource_id, {"enabled": enabled}
        )

    async def delete_resource(
        self, provider_id: str, resource_id: str
    ) -> None:
        if self._repository_backed:
            return await self._repository_delete(provider_id, resource_id)
        self._legacy_provider_guard(provider_id)
        return await self._legacy_delete(resource_id)

    async def _legacy_create(
        self, payload: Dict[str, Any]
    ) -> AntigravityResource:
        if "id" not in payload:
            raise ResourceManagementError("resource id is required")
        self._reject_secret_fields(payload)
        values = {
            field: payload[field]
            for field in _CREATE_FIELDS + _EDITABLE_FIELDS
            if field in payload
        }
        values["id"] = payload["id"]
        self._validate_values(values, creating=True)
        values["id"] = values["id"].strip()
        values.setdefault("provider", "antigravity")
        values.setdefault("ide_type", "ANTIGRAVITY")

        resource = AntigravityResource(**values)
        async with self._lock:
            pool = self._require_pool()
            if any(item.id == resource.id for item in pool.resources):
                raise ResourceManagementError("resource already exists")
            self._validate_credential_reference(values)
            try:
                await pool.add_resource(resource)
            except ValueError as exc:
                raise ResourceManagementError("resource already exists") from exc
            self._original_values[resource.id] = self._resource_values(resource)
            try:
                self._persist()
            except Exception:
                await pool.remove_resource(resource)
                self._original_values.pop(resource.id, None)
                raise
            # CONFIG/R-2-C: the definition is live — invalidate discovery.
            self._invalidate_model_registry()
        return resource

    async def _legacy_update(
        self, resource_id: str, payload: Dict[str, Any]
    ) -> AntigravityResource:
        self._reject_secret_fields(payload)
        unknown = set(payload) - set(_EDITABLE_FIELDS)
        if unknown:
            raise ResourceManagementError("resource contains unsupported fields")
        self._validate_values(payload, creating=False)

        async with self._lock:
            resource = self.get_resource("antigravity", resource_id)
            before = self._resource_values(resource)
            # AUTH-016 B: the legacy in-place bind validates too —
            # before any mutation, so a failure leaves no residue.
            self._validate_credential_reference(payload)
            for field, value in payload.items():
                setattr(resource, field, value)
            try:
                self._persist()
            except Exception:
                for field, value in before.items():
                    setattr(resource, field, value)
                raise
            self._original_values[resource.id] = self._resource_values(resource)
            # CONTROL-006-FIX-1: a legacy credential rebind happens in
            # place — the provider's per-resource OAuth state must not
            # survive it (same semantics as the repository path).
            # CONTROL-007-DECISION-001: the same boundary resets the
            # scheduling state — rate limits belong to the credential —
            # while the observability counters keep accumulating.
            if "credential_id" in payload and (
                before.get("credential_id") != payload["credential_id"]
            ):
                resource.health = HealthState.HEALTHY
                resource.cooldown_until = None
                resource.consecutive_failures = 0
                await self._invalidate_provider_adapters(
                    "antigravity", (resource_id,)
                )
            # CONFIG/R-2-C: the definition is live — invalidate discovery.
            self._invalidate_model_registry()
            return resource

    async def _legacy_delete(self, resource_id: str) -> None:
        async with self._lock:
            pool = self._require_pool()
            resource = self.get_resource("antigravity", resource_id)
            try:
                await pool.remove_resource(resource)
            except ValueError as exc:
                if resource.in_flight:
                    raise ResourceManagementError(
                        "resource is in flight"
                    ) from exc
                raise ResourceManagementError("resource not found") from exc
            original = self._original_values.pop(resource.id, None)
            try:
                self._persist()
            except Exception:
                await pool.add_resource(resource)
                if original is not None:
                    self._original_values[resource.id] = original
                raise
            # CONTROL-006-FIX-1: the resource is gone — its provider
            # auth-adapter OAuth state must not survive a same-id recreate.
            self._invalidate_provider_adapters("antigravity", (resource_id,))
            # CONFIG/R-2-C: the definition is gone — invalidate discovery.
            self._invalidate_model_registry()

    def _persist(self) -> None:
        """Persist live resources without expanding environment markers."""
        raw = copy.deepcopy(self._source_config)
        providers = raw.setdefault("providers", {})
        antigravity = providers.setdefault("antigravity", {})
        source_items = antigravity.get("resources")
        source_by_id = {
            item.get("id"): item
            for item in (source_items if isinstance(source_items, list) else [])
            if isinstance(item, dict)
        }

        serialized: list[dict[str, Any]] = []
        for resource in self._resources():
            source = source_by_id.get(resource.id, {})
            item: dict[str, Any] = {
                "id": resource.id,
                "provider": "antigravity",
            }
            for field in _PERSISTED_FIELDS:
                if field in ("id", "provider"):
                    continue
                if self._durable_credentials and field in _LEGACY_CREDENTIAL_FIELDS:
                    # WEBUI-001: durable CredentialRepository owns the long-lived
                    # material.  Never re-derive it from the live Resource, but do
                    # not wipe pre-existing legacy keys out of config.yaml either
                    # -- the startup migration path (AUTH-010) still consumes them
                    # and this task deliberately does not widen that scope.
                    if field in source:
                        item[field] = source[field]
                    continue
                current = getattr(resource, field, None)
                original = self._original_values.get(resource.id, {}).get(field)
                if field in source and current == original:
                    # Preserve ${ENV_VAR} instead of writing the resolved
                    # credential back into the configuration file.
                    item[field] = source[field]
                elif current is not None:
                    item[field] = current
            serialized.append(item)
        antigravity["resources"] = serialized

        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{self.config_path.name}.",
                suffix=".tmp",
                dir=str(self.config_path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    yaml.safe_dump(raw, handle, allow_unicode=True, sort_keys=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.chmod(temp_name, 0o600)
                except OSError:
                    pass
                os.replace(temp_name, self.config_path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            try:
                os.chmod(self.config_path, 0o600)
            except OSError:
                pass
        except (OSError, yaml.YAMLError) as exc:
            raise ResourceManagementError(
                "resource configuration cannot be written"
            ) from exc
        self._source_config = raw

