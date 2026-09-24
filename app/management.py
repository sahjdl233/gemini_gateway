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
from providers.antigravity.resource import AntigravityResource


_EDITABLE_FIELDS = (
    "enabled",
    "access_token",
    "refresh_token",
    "client_id",
    "client_secret",
    "project_id",
    "ide_type",
)
_PERSISTED_FIELDS = _EDITABLE_FIELDS + ("id", "provider", "token_expiry")


class ResourceManagementError(Exception):
    """A safe management error whose message never contains credentials."""


def _configured(value: Any) -> str | None:
    return "configured" if value else None


class ResourceManager:
    """Manage the live Antigravity resources and the existing YAML config."""

    def __init__(
        self,
        scheduler: Any,
        config: Dict[str, Any],
        config_path: Path | str = Path("config.yaml"),
    ) -> None:
        self.scheduler = scheduler
        self.config = config
        self.config_path = Path(config_path)
        pools = getattr(scheduler, "pools", {})
        self.pool: InMemoryPool | None = pools.get("antigravity")
        self._lock = asyncio.Lock()
        self._source_config = self._read_source()
        self._original_values: Dict[str, Dict[str, Any]] = {
            resource.id: self._resource_values(resource)
            for resource in self._resources()
        }

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
            "health": health,
            "cooldown_until": cooldown,
            "in_flight": resource.in_flight,
            "total_requests": resource.total_requests,
            "total_failures": resource.total_failures,
            "project_id": resource.project_id,
            "ide_type": resource.ide_type,
            "access_token": _configured(resource.access_token),
            "refresh_token": _configured(resource.refresh_token),
            "client_id": _configured(resource.client_id),
            "client_secret": _configured(resource.client_secret),
        }

    @staticmethod
    def _validate_values(values: Dict[str, Any], *, creating: bool) -> None:
        if creating:
            resource_id = values.get("id")
            if not isinstance(resource_id, str) or not resource_id.strip():
                raise ResourceManagementError("resource id is required")

        if "enabled" in values and not isinstance(values["enabled"], bool):
            raise ResourceManagementError("enabled must be a boolean")

        for field in _EDITABLE_FIELDS:
            if field == "enabled":
                continue
            value = values.get(field)
            if value is not None and not isinstance(value, str):
                raise ResourceManagementError(f"{field} must be a string")

    async def create_resource(
        self, payload: Dict[str, Any]
    ) -> AntigravityResource:
        if "id" not in payload:
            raise ResourceManagementError("resource id is required")
        values = {
            field: payload[field]
            for field in _EDITABLE_FIELDS
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

    async def update_resource(
        self, resource_id: str, payload: Dict[str, Any]
    ) -> AntigravityResource:
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

    async def set_enabled(
        self, resource_id: str, enabled: bool
    ) -> AntigravityResource:
        return await self.update_resource(resource_id, {"enabled": enabled})

    async def delete_resource(self, resource_id: str) -> None:
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

