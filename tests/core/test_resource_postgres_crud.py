"""PostgreSQLResourceRepository CRUD tests (DB-RESOURCE-001-4).

The suite mirrors the DB-RESOURCE-001-2 contract semantics against the
PostgreSQL implementation, using an in-memory async fake connection over
a keyed table (pattern from ``tests/core/_fake_postgres.py``):

* writes land in a per-connection overlay and reach the shared table
  only on ``commit()``; ``rollback()`` discards them;
* the PRIMARY KEY (provider, resource_id) raises a fake driver error
  with ``sqlstate == "23505"`` on duplicate INSERT;
* the JSONB ``definition`` column round-trips through
  ``json.dumps`` on write and a dict on read, like psycopg.

The fake verifies the exact SQL the repository sends (statement
constants, not free-form parsing) and records transactional behaviour so
tests can assert commit / rollback / close per operation.

Real-database integration, if added later, must follow the
``GEMINI_GATEWAY_TEST_DATABASE_URL`` opt-in convention.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import pytest

from core.resource_definition import (
    AntigravityResourceDefinition,
    GeminiCliResourceDefinition,
    ResourceDefinitionBase,
)
from core.resource_postgres import (
    _DELETE_DEFINITION_SQL,
    _INSERT_DEFINITION_SQL,
    _LIST_ALL_SQL,
    _LIST_BY_PROVIDER_SQL,
    _SELECT_BY_KEY_SQL,
    _UPDATE_DEFINITION_SQL,
    PostgreSQLResourceRepository,
)
from core.resource_repository import (
    DuplicateResourceDefinitionError,
    UnknownResourceDefinitionError,
)


class FakeUniqueViolation(Exception):
    """Stand-in for a driver unique-violation error (DBAPI SQLSTATE)."""

    sqlstate = "23505"


class FakeAsyncCursor:
    def __init__(self, rows: List[Dict[str, Any]], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    async def fetchone(self) -> Optional[Dict[str, Any]]:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> List[Dict[str, Any]]:
        return list(self._rows)


Key = Tuple[str, str]


class FakeAsyncConnection:
    """One transaction over the shared ``resource_definitions`` table."""

    def __init__(self, table: Dict[Key, Dict[str, Any]]) -> None:
        self._table = table
        self._pending: Dict[Key, Optional[Dict[str, Any]]] = {}
        self.executed: List[str] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.closed = False
        self.fail_next_execute: Optional[Exception] = None
        self.fail_on_commit: Optional[Exception] = None

    def _row(self, key: Key) -> Optional[Dict[str, Any]]:
        if key in self._pending:
            return self._pending[key]
        return self._table.get(key)

    @staticmethod
    def _as_jsonb(row: Dict[str, Any]) -> Dict[str, Any]:
        """Emulate a JSONB column: the driver deserializes to a dict."""
        row = dict(row)
        if isinstance(row.get("definition"), str):
            row["definition"] = json.loads(row["definition"])
        return row

    async def execute(
        self, sql: str, params: Optional[tuple] = None
    ) -> FakeAsyncCursor:
        statement = " ".join(sql.split())
        self.executed.append(statement)
        if self.fail_next_execute is not None:
            exc = self.fail_next_execute
            self.fail_next_execute = None
            raise exc

        if statement == " ".join(_INSERT_DEFINITION_SQL.split()):
            provider, resource_id, enabled, credential_id, body = params
            key = (provider, resource_id)
            if self._row(key) is not None:
                raise FakeUniqueViolation(
                    "duplicate key value violates unique constraint"
                )
            self._pending[key] = {
                "provider": provider,
                "resource_id": resource_id,
                "enabled": enabled,
                "credential_id": credential_id,
                "definition": body,
            }
            return FakeAsyncCursor([], 1)
        if statement == " ".join(_SELECT_BY_KEY_SQL.split()):
            provider, resource_id = params
            row = self._row((provider, resource_id))
            return FakeAsyncCursor(
                [self._as_jsonb(row)] if row else [], 1 if row else 0
            )
        if statement == " ".join(_LIST_ALL_SQL.split()):
            visible = self._visible()
            rows = sorted(
                visible.values(),
                key=lambda r: (r["provider"], r["resource_id"]),
            )
            return FakeAsyncCursor(
                [self._as_jsonb(r) for r in rows], len(rows)
            )
        if statement == " ".join(_LIST_BY_PROVIDER_SQL.split()):
            (provider,) = params
            rows = sorted(
                (
                    self._as_jsonb(r)
                    for r in self._visible().values()
                    if r["provider"] == provider
                ),
                key=lambda r: r["resource_id"],
            )
            return FakeAsyncCursor(list(rows), len(rows))
        if statement == " ".join(_UPDATE_DEFINITION_SQL.split()):
            enabled, credential_id, body, provider, resource_id = params
            key = (provider, resource_id)
            row = self._row(key)
            if row is None:
                return FakeAsyncCursor([], 0)
            row = dict(row)
            row["enabled"] = enabled
            row["credential_id"] = credential_id
            row["definition"] = body
            self._pending[key] = row
            return FakeAsyncCursor([self._as_jsonb(row)], 1)
        if statement == " ".join(_DELETE_DEFINITION_SQL.split()):
            provider, resource_id = params
            key = (provider, resource_id)
            if self._row(key) is None:
                return FakeAsyncCursor([], 0)
            self._pending[key] = None  # tombstone
            return FakeAsyncCursor([], 1)
        raise AssertionError(f"fake driver received unexpected SQL: {statement}")

    def _visible(self) -> Dict[Key, Dict[str, Any]]:
        visible: Dict[Key, Dict[str, Any]] = dict(self._table)
        for key, row in self._pending.items():
            if row is None:
                visible.pop(key, None)
            else:
                visible[key] = row
        return visible

    async def commit(self) -> None:
        if self.fail_on_commit is not None:
            exc = self.fail_on_commit
            self.fail_on_commit = None
            raise exc
        for key, row in self._pending.items():
            if row is None:
                self._table.pop(key, None)
            else:
                self._table[key] = row
        self._pending.clear()
        self.commit_calls += 1

    async def rollback(self) -> None:
        self._pending.clear()
        self.rollback_calls += 1

    async def close(self) -> None:
        self.closed = True


class FakeAsyncPostgres:
    """Harness: one shared table, one fresh connection per operation."""

    def __init__(self) -> None:
        self.table: Dict[Key, Dict[str, Any]] = {}
        self.connections: List[FakeAsyncConnection] = []
        self.next_connection: FakeAsyncConnection = FakeAsyncConnection(self.table)

    def connection_factory(self):
        async def factory() -> Any:
            connection = self.next_connection
            self.next_connection = FakeAsyncConnection(self.table)
            self.connections.append(connection)
            return connection

        return factory

    @property
    def last_connection(self) -> FakeAsyncConnection:
        return self.connections[-1]

    def raw_rows(self) -> Dict[Key, Dict[str, Any]]:
        """Direct view of the COMMITTED table (bypasses the repository)."""
        return self.table


def normalize(sql: str) -> str:
    return " ".join(sql.split())


@pytest.fixture
def fake_db() -> FakeAsyncPostgres:
    return FakeAsyncPostgres()


@pytest.fixture
def repo(fake_db: FakeAsyncPostgres) -> PostgreSQLResourceRepository:
    return PostgreSQLResourceRepository(fake_db.connection_factory())


def antigravity_def(
    rid: str = "r1",
    project_id: str = "proj-1",
) -> AntigravityResourceDefinition:
    return AntigravityResourceDefinition(
        id=rid,
        enabled=True,
        credential_id="cred-1",
        project_id=project_id,
    )


def gemini_def(
    rid: str = "r2",
    project_id: str = "proj-gemini",
) -> GeminiCliResourceDefinition:
    return GeminiCliResourceDefinition(
        id=rid,
        enabled=False,
        credential_id=None,
        project_id=project_id,
    )


# -- add ------------------------------------------------------------------------


async def test_add_inserts_and_returns_definition(repo, fake_db):
    defn = antigravity_def()
    saved = await repo.add(defn)
    assert saved is defn
    # Exactly one connection, exactly the INSERT, committed, closed.
    connection = fake_db.last_connection
    assert connection.executed == [normalize(_INSERT_DEFINITION_SQL)]
    assert connection.commit_calls == 1
    assert connection.rollback_calls == 0
    assert connection.closed is True
    # The committed row carries the composite key, the column fields and
    # the JSONB definition body (no common fields inside the body).
    row = fake_db.raw_rows()[("antigravity", "r1")]
    assert row["provider"] == "antigravity"
    assert row["resource_id"] == "r1"
    assert row["enabled"] is True
    assert row["credential_id"] == "cred-1"
    # JSONB column holds the serialized definition body (no common fields).
    assert json.loads(row["definition"]) == {
        "project_id": "proj-1",
        "ide_type": "ANTIGRAVITY",
    }


async def test_add_duplicate_maps_to_typed_error(repo, fake_db):
    await repo.add(antigravity_def())
    with pytest.raises(DuplicateResourceDefinitionError) as exc:
        await repo.add(antigravity_def(project_id="proj-other"))
    # Typed error carries the composite identity; psycopg IntegrityError
    # never escapes the repository.
    assert exc.value.provider == "antigravity"
    assert exc.value.resource_id == "r1"
    assert "antigravity" in str(exc.value)
    assert "r1" in str(exc.value)
    # Duplicate write was rolled back, connection closed.
    connection = fake_db.last_connection
    assert connection.rollback_calls == 1
    assert connection.commit_calls == 0
    assert connection.closed is True
    # The original row is untouched.
    assert json.loads(
        fake_db.raw_rows()[("antigravity", "r1")]["definition"]
    ) == {"project_id": "proj-1", "ide_type": "ANTIGRAVITY"}


async def test_add_failure_propagates_unwrapped(repo, fake_db):
    fake_db.next_connection.fail_next_execute = RuntimeError("disk on fire")
    with pytest.raises(RuntimeError, match="disk on fire"):
        await repo.add(antigravity_def())
    connection = fake_db.last_connection
    assert connection.rollback_calls == 1
    assert connection.closed is True


# -- get ------------------------------------------------------------------------


async def test_get_found_round_trips_dto(repo, fake_db):
    await repo.add(gemini_def())
    result = await repo.get("gemini_cli", "r2")
    assert isinstance(result, GeminiCliResourceDefinition)
    assert result.provider == "gemini_cli"
    assert result.id == "r2"
    assert result.enabled is False
    assert result.credential_id is None
    assert result.project_id == "proj-gemini"


async def test_get_missing_returns_none(repo, fake_db):
    await repo.add(antigravity_def())
    # Different resource_id and different provider both miss.
    assert await repo.get("antigravity", "missing") is None
    assert await repo.get("gemini_cli", "r1") is None


# -- require ----------------------------------------------------------------------


async def test_require_found_returns_definition(repo, fake_db):
    await repo.add(antigravity_def(rid="r9"))
    result = await repo.require("antigravity", "r9")
    assert isinstance(result, AntigravityResourceDefinition)
    assert result.id == "r9"


async def test_require_missing_raises_typed_error(repo, fake_db):
    with pytest.raises(UnknownResourceDefinitionError) as exc:
        await repo.require("antigravity", "missing")
    assert exc.value.provider == "antigravity"
    assert exc.value.resource_id == "missing"
    assert "antigravity" in str(exc.value)
    assert "missing" in str(exc.value)


# -- list -----------------------------------------------------------------------


async def test_list_all_deterministic_order(repo, fake_db):
    await repo.add(antigravity_def(rid="b"))
    await repo.add(antigravity_def(rid="a"))
    await repo.add(gemini_def(rid="z"))
    await repo.add(antigravity_def(rid="c"))

    items = await repo.list()
    assert [(d.provider, d.id) for d in items] == [
        ("antigravity", "a"),
        ("antigravity", "b"),
        ("antigravity", "c"),
        ("gemini_cli", "z"),
    ]
    # Order is decided by the SQL statement, not the fake's sort alone:
    # the unfiltered listing must have used ORDER BY provider, resource_id.
    assert fake_db.last_connection.executed == [normalize(_LIST_ALL_SQL)]


async def test_list_filtered_by_provider(repo, fake_db):
    await repo.add(antigravity_def(rid="a"))
    await repo.add(gemini_def(rid="r2"))
    await repo.add(antigravity_def(rid="b"))

    items = await repo.list(provider="antigravity")
    assert [d.id for d in items] == ["a", "b"]
    assert all(d.provider == "antigravity" for d in items)
    # Provider filter uses the dedicated statement ordered by resource_id.
    assert fake_db.last_connection.executed == [
        normalize(_LIST_BY_PROVIDER_SQL)
    ]


async def test_list_provider_with_no_rows_is_empty(repo, fake_db):
    await repo.add(antigravity_def())
    assert await repo.list(provider="firebase") == []


# -- update ----------------------------------------------------------------------


async def test_update_replaces_full_row(repo, fake_db):
    await repo.add(antigravity_def(rid="r1", project_id="proj-original"))
    updated = AntigravityResourceDefinition(
        id="r1",
        enabled=False,
        credential_id="cred-new",
        project_id="proj-updated",
    )
    result = await repo.update(updated)
    assert result is updated
    connection = fake_db.last_connection
    assert connection.executed == [normalize(_UPDATE_DEFINITION_SQL)]
    assert connection.commit_calls == 1
    # Full replacement: every mutable column reflects the new DTO.
    row = fake_db.raw_rows()[("antigravity", "r1")]
    assert row["enabled"] is False
    assert row["credential_id"] == "cred-new"
    assert json.loads(row["definition"]) == {
        "project_id": "proj-updated",
        "ide_type": "ANTIGRAVITY",
    }
    # And the repository returns the replaced shape on read.
    retrieved = await repo.get("antigravity", "r1")
    assert retrieved.project_id == "proj-updated"
    assert retrieved.enabled is False
    assert retrieved.credential_id == "cred-new"


async def test_update_missing_raises_not_upsert(repo, fake_db):
    updated = antigravity_def(rid="ghost", project_id="proj-new")
    with pytest.raises(UnknownResourceDefinitionError) as exc:
        await repo.update(updated)
    assert exc.value.provider == "antigravity"
    assert exc.value.resource_id == "ghost"
    # Nothing was written (no upsert fallback): the table stays empty.
    assert fake_db.raw_rows() == {}
    connection = fake_db.last_connection
    assert connection.rollback_calls == 1
    assert connection.commit_calls == 0


# -- delete ----------------------------------------------------------------------


async def test_delete_existing_removes_row(repo, fake_db):
    await repo.add(antigravity_def())
    await repo.delete("antigravity", "r1")
    assert await repo.get("antigravity", "r1") is None
    assert fake_db.raw_rows() == {}


async def test_delete_missing_is_idempotent(repo, fake_db):
    await repo.add(antigravity_def())
    # Unknown key and repeat delete after removal: both no-ops, no raise.
    await repo.delete("antigravity", "nope")
    await repo.delete("antigravity", "r1")
    await repo.delete("antigravity", "r1")
    connection = fake_db.last_connection
    assert connection.executed == [normalize(_DELETE_DEFINITION_SQL)]
    assert connection.commit_calls == 1
    assert connection.rollback_calls == 0
    assert await repo.get("antigravity", "r1") is None


# -- transactional hygiene on every operation -------------------------------------


async def test_every_operation_closes_its_connection(repo, fake_db):
    await repo.add(antigravity_def())
    await repo.get("antigravity", "r1")
    await repo.require("antigravity", "r1")
    await repo.list()
    await repo.list(provider="antigravity")
    await repo.update(antigravity_def(rid="r1", project_id="proj-2"))
    await repo.delete("antigravity", "r1")
    assert len(fake_db.connections) == 7
    for connection in fake_db.connections:
        assert connection.closed is True
        assert connection.rollback_calls == 0
