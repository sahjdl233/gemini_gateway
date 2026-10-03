"""Runtime management for the existing Antigravity ``InMemoryPool``.

This module deliberately keeps management state inside the existing Resource
objects and pool. It is not a second resource model or scheduler.
"""

from __future__ import annotations

import asyncio
import copy
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable

import yaml

from core.pool import InMemoryPool
from core.resource_definition import (
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
    ) -> None:
        self.scheduler = scheduler
        self.config = config
        self.config_path = Path(config_path)
        self._repository = repository
        self._runtime_builder = runtime_builder
        pools = getattr(scheduler, "pools", {})
        self.pool: InMemoryPool | None = pools.get("antigravity")
        self._lock = asyncio.Lock()
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

    def get_resource(self, resource_id: str) -> AntigravityResource:
        pool = self._require_pool()
        for resource in pool.resources:
            if (
                isinstance(resource, AntigravityResource)
                and resource.id == resource_id
            ):
                return resource
        raise ResourceManagementError("resource not found")

    def list_resources(self) -> list[dict[str, Any]]:
        return [self.serialize(resource) for resource in self._resources()]

    def serialize(self, resource: AntigravityResource) -> dict[str, Any]:
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
            "project_id": resource.project_id,
            "ide_type": resource.ide_type,
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

    # -- DB-RESOURCE-013: repository-backed write path -------------------

    async def _reconcile_runtime(self) -> None:
        """Rebuild runtime resources from the repository and apply them
        to every pool.  Runtime state is preserved per ResourceKey by
        ``pool.reconcile_resources``; in-flight violations propagate as
        management errors."""
        service = RuntimeReconciliationService(
            ResourceRepositoryDefinitionSource(self._repository),
            runtime_builder=self._runtime_builder,
        )
        snapshot = await service.reconcile()
        try:
            for provider_id, pool in self.scheduler.pools.items():
                await pool.reconcile_resources(
                    snapshot.resources_by_provider.get(provider_id, [])
                )
        except RuntimeError as exc:
            raise ResourceManagementError(str(exc)) from exc

    def _definition_payload_from_resource(
        self, resource: AntigravityResource
    ) -> Dict[str, Any]:
        """Runtime resource → strict definition payload (Admin scope)."""
        return {
            "provider": "antigravity",
            "id": resource.id,
            "enabled": resource.enabled,
            "credential_id": resource.credential_id,
            "project_id": resource.project_id,
            "ide_type": resource.ide_type,
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
        self, payload: Dict[str, Any]
    ) -> AntigravityResource:
        values = dict(payload)
        values["provider"] = "antigravity"
        definition = self._parse_definition(values)
        try:
            await self._repository.add(definition)
        except DuplicateResourceDefinitionError:
            raise ResourceManagementError("resource already exists") from None
        try:
            await self._reconcile_runtime()
        except ResourceManagementError:
            # Compensate: the store write is rolled back so the store and
            # the runtime stay consistent with each other.
            await self._repository.delete(definition.provider, definition.id)
            raise
        return self.get_resource(definition.id)

    async def _repository_update(
        self, resource_id: str, payload: Dict[str, Any]
    ) -> AntigravityResource:
        current = await self._repository.get("antigravity", resource_id)
        if current is None:
            # Defensive: runtime resource exists but no stored definition
            # (should not happen on the repository path) — derive one
            # from the live resource so the write is still full-replacement.
            resource = self.get_resource(resource_id)
            base = self._definition_payload_from_resource(resource)
        else:
            base = current.to_runtime_definition()
        payload_resource_id = payload.get("id")
        if payload_resource_id is not None and payload_resource_id != resource_id:
            raise ResourceManagementError("resource id cannot be changed")
        base.update(payload)
        base["provider"] = "antigravity"
        base["id"] = resource_id
        definition = self._parse_definition(base)
        try:
            await self._repository.update(definition)
        except UnknownResourceDefinitionError:
            raise ResourceManagementError("resource not found") from None
        try:
            await self._reconcile_runtime()
        except ResourceManagementError:
            raise
        return self.get_resource(resource_id)

    async def _repository_delete(self, resource_id: str) -> None:
        # Existence check against the live pool for the friendly 404; the
        # repository delete itself is idempotent.
        self.get_resource(resource_id)
        await self._repository.delete("antigravity", resource_id)
        try:
            await self._reconcile_runtime()
        except ResourceManagementError:
            raise

    async def create_resource(
        self, payload: Dict[str, Any]
    ) -> AntigravityResource:
        if self._repository_backed:
            if "id" not in payload:
                raise ResourceManagementError("resource id is required")
            self._reject_secret_fields(payload)
            return await self._repository_create(payload)
        return await self._legacy_create(payload)

    async def update_resource(
        self, resource_id: str, payload: Dict[str, Any]
    ) -> AntigravityResource:
        if self._repository_backed:
            self._reject_secret_fields(payload)
            unknown = set(payload) - set(_EDITABLE_FIELDS)
            if unknown:
                raise ResourceManagementError(
                    "resource contains unsupported fields"
                )
            self._validate_values(payload, creating=False)
            return await self._repository_update(resource_id, payload)
        return await self._legacy_update(resource_id, payload)

    async def set_enabled(
        self, resource_id: str, enabled: bool
    ) -> AntigravityResource:
        return await self.update_resource(resource_id, {"enabled": enabled})

    async def delete_resource(self, resource_id: str) -> None:
        if self._repository_backed:
            return await self._repository_delete(resource_id)
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
            resource = self.get_resource(resource_id)
            before = self._resource_values(resource)
            for field, value in payload.items():
                setattr(resource, field, value)
            try:
                self._persist()
            except Exception:
                for field, value in before.items():
                    setattr(resource, field, value)
                raise
            self._original_values[resource.id] = self._resource_values(resource)
            return resource

    async def _legacy_delete(self, resource_id: str) -> None:
        async with self._lock:
            pool = self._require_pool()
            resource = self.get_resource(resource_id)
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

