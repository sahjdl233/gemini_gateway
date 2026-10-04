"""PostgreSQL resource definition repository (CONFIG/R-6)."""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from core.resource_definition import resource_definition_from_row
from core.repositories.resource_definition import (
    ResourceDefinition,
    ResourceDefinitionRepository,
)

__all__ = ["PostgresResourceDefinitionRepository"]

_SELECT_ALL_SQL = (
    "SELECT provider, resource_id, enabled, credential_id, definition "
    "FROM resource_definitions ORDER BY provider, resource_id"
)
_SELECT_BY_KEY_SQL = (
    "SELECT provider, resource_id, enabled, credential_id, definition "
    "FROM resource_definitions WHERE provider = %s AND resource_id = %s"
)
_UPSERT_SQL = (
    "INSERT INTO resource_definitions "
    "(provider, resource_id, enabled, credential_id, definition) "
    "VALUES (%s, %s, %s, %s, %s) "
    "ON CONFLICT (provider, resource_id) DO UPDATE SET "
    "enabled = EXCLUDED.enabled, "
    "credential_id = EXCLUDED.credential_id, "
    "definition = EXCLUDED.definition, "
    "updated_at = now()"
)
_DELETE_SQL = (
    "DELETE FROM resource_definitions WHERE provider = %s AND resource_id = %s"
)


class PostgresResourceDefinitionRepository(ResourceDefinitionRepository):
    """``ResourceDefinitionRepository`` over ``resource_definitions``.

    ``connection_factory`` is a zero-arg async callable returning a fresh
    duck-typed connection per operation (commit on success, rollback on
    error, always closed).  ``initialize()`` runs the shared idempotent
    migration (safe to call on every startup).
    """

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connection_factory = connection_factory

    async def initialize(self) -> None:
        from core.repositories.postgres.schema import initialize_persistence

        await initialize_persistence(self._connection_factory)

    @staticmethod
    def _row_to_definition(row: Any) -> ResourceDefinition:
        return resource_definition_from_row(
            provider=row["provider"],
            resource_id=row["resource_id"],
            enabled=row["enabled"],
            credential_id=row["credential_id"],
            definition=row["definition"],
        )

    async def list_all(self) -> List[ResourceDefinition]:
        connection = await self._connection_factory()
        try:
            cursor = await connection.execute(_SELECT_ALL_SQL)
            rows = await cursor.fetchall()
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        finally:
            await connection.close()
        return [self._row_to_definition(row) for row in rows]

    async def get(
        self, provider: str, resource_id: str
    ) -> Optional[ResourceDefinition]:
        connection = await self._connection_factory()
        try:
            cursor = await connection.execute(
                _SELECT_BY_KEY_SQL, (provider, resource_id)
            )
            row = await cursor.fetchone()
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        finally:
            await connection.close()
        if row is None:
            return None
        return self._row_to_definition(row)

    async def save(self, definition: ResourceDefinition) -> None:
        import json

        connection = await self._connection_factory()
        try:
            await connection.execute(
                _UPSERT_SQL,
                (
                    definition.provider,
                    definition.id,
                    definition.enabled,
                    definition.credential_id,
                    json.dumps(definition.to_definition_json()),
                ),
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        finally:
            await connection.close()

    async def delete(self, provider: str, resource_id: str) -> None:
        connection = await self._connection_factory()
        try:
            await connection.execute(_DELETE_SQL, (provider, resource_id))
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        finally:
            await connection.close()
