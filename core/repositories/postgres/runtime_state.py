"""PostgreSQL runtime state store (CONFIG/R-6)."""

from __future__ import annotations

from typing import Any, Callable, Optional

from core.health import HealthState
from core.repositories.runtime_state import RuntimeState, RuntimeStateStore

__all__ = ["PostgresRuntimeStateStore"]

_SELECT_SQL = (
    "SELECT provider, resource_id, health, cooldown_until, "
    "consecutive_failures, total_requests, total_failures "
    "FROM runtime_state WHERE provider = %s AND resource_id = %s"
)
_UPSERT_SQL = (
    "INSERT INTO runtime_state "
    "(provider, resource_id, health, cooldown_until, "
    "consecutive_failures, total_requests, total_failures, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, now()) "
    "ON CONFLICT (provider, resource_id) DO UPDATE SET "
    "health = EXCLUDED.health, "
    "cooldown_until = EXCLUDED.cooldown_until, "
    "consecutive_failures = EXCLUDED.consecutive_failures, "
    "total_requests = EXCLUDED.total_requests, "
    "total_failures = EXCLUDED.total_failures, "
    "updated_at = now()"
)


class PostgresRuntimeStateStore(RuntimeStateStore):
    """``RuntimeStateStore`` over the ``runtime_state`` table.

    The store is optional by contract: nothing in startup reads it, and
    a missing row simply means "no recorded state".
    """

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connection_factory = connection_factory

    async def initialize(self) -> None:
        from core.repositories.postgres.schema import initialize_persistence

        await initialize_persistence(self._connection_factory)

    async def get(self, resource_key) -> Optional[RuntimeState]:
        connection = await self._connection_factory()
        try:
            cursor = await connection.execute(
                _SELECT_SQL,
                (resource_key.provider, resource_key.id),
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
        return RuntimeState(
            resource_key=resource_key,
            health=HealthState(row["health"]),
            cooldown_until=row["cooldown_until"],
            consecutive_failures=row["consecutive_failures"],
            total_requests=row["total_requests"],
            total_failures=row["total_failures"],
        )

    async def save(self, state: RuntimeState) -> None:
        connection = await self._connection_factory()
        try:
            await connection.execute(
                _UPSERT_SQL,
                (
                    state.resource_key.provider,
                    state.resource_key.id,
                    state.health.value,
                    state.cooldown_until,
                    state.consecutive_failures,
                    state.total_requests,
                    state.total_failures,
                ),
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        finally:
            await connection.close()
