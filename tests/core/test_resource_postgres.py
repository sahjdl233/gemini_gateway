"""PostgreSQLResourceRepository schema + initialize tests (DB-RESOURCE-001-3).

Unit tests run entirely against an in-memory async connection fake — no
local PostgreSQL required.  The fake mirrors the duck-typed *async*
connection surface the repository relies on (``await execute`` /
``commit`` / ``rollback`` / ``close``) and records every executed
statement so tests can assert exactly what SQL ran (and that nothing
destructive ever does).

Real-database integration, if added later, must follow the
``GEMINI_GATEWAY_TEST_DATABASE_URL`` opt-in convention.
"""

from __future__ import annotations

from typing import Any, List, Optional

import pytest

from core.resource_postgres import (
    RESOURCE_DEFINITIONS_SCHEMA_SQL,
    PostgreSQLResourceRepository,
)


class FakeAsyncCursor:
    def __init__(self, rows: List[dict], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    async def fetchone(self) -> Optional[dict]:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> List[dict]:
        return list(self._rows)


class FakeAsyncConnection:
    """Async duck-typed connection recording every executed statement."""

    def __init__(self) -> None:
        self.executed: List[str] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.closed = False
        self.fail_next_execute: Optional[Exception] = None
        self.fail_on_commit: Optional[Exception] = None

    async def execute(
        self, sql: str, params: Optional[tuple] = None
    ) -> FakeAsyncCursor:
        statement = " ".join(sql.split())
        self.executed.append(statement)
        if self.fail_next_execute is not None:
            exc = self.fail_next_execute
            self.fail_next_execute = None
            raise exc
        return FakeAsyncCursor([], 0)

    async def commit(self) -> None:
        if self.fail_on_commit is not None:
            exc = self.fail_on_commit
            self.fail_on_commit = None
            raise exc
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1

    async def close(self) -> None:
        self.closed = True


class FakeAsyncPostgres:
    """Harness: one pre-created connection per initialize() call.

    The connection the next ``initialize()`` will receive is created
    eagerly as ``next_connection`` so tests can preconfigure failure
    behaviour before any repository call.  Each connection is used at
    most once; afterwards it lands in ``connections`` for inspection.
    """

    def __init__(self) -> None:
        self.connections: List[FakeAsyncConnection] = []
        self.next_connection: FakeAsyncConnection = FakeAsyncConnection()

    def connection_factory(self):
        async def factory() -> Any:
            connection = self.next_connection
            self.next_connection = FakeAsyncConnection()
            self.connections.append(connection)
            return connection

        return factory

    @property
    def last_connection(self) -> FakeAsyncConnection:
        return self.connections[-1]


DESTRUCTIVE_KEYWORDS = ("DROP ", "ALTER ", "TRUNCATE ", "DELETE ", "UPDATE ")


def normalize(sql: str) -> str:
    return " ".join(sql.split())


@pytest.fixture
def fake_db() -> FakeAsyncPostgres:
    return FakeAsyncPostgres()


@pytest.fixture
def repo(fake_db: FakeAsyncPostgres) -> PostgreSQLResourceRepository:
    return PostgreSQLResourceRepository(fake_db.connection_factory())


# -- schema contract ----------------------------------------------------------------


def test_schema_contains_all_columns_with_types_and_nullability():
    sql = normalize(RESOURCE_DEFINITIONS_SCHEMA_SQL)
    assert "provider TEXT NOT NULL" in sql
    assert "resource_id TEXT NOT NULL" in sql
    assert "enabled BOOLEAN NOT NULL" in sql
    assert "credential_id TEXT NULL" in sql
    assert "definition JSONB NOT NULL" in sql


def test_schema_uses_composite_primary_key():
    sql = normalize(RESOURCE_DEFINITIONS_SCHEMA_SQL)
    assert "CREATE TABLE IF NOT EXISTS resource_definitions" in sql
    assert "PRIMARY KEY (provider, resource_id)" in sql


def test_schema_has_no_standalone_unique_on_resource_id():
    sql = normalize(RESOURCE_DEFINITIONS_SCHEMA_SQL)
    assert "resource_id TEXT NOT NULL UNIQUE" not in sql
    assert "UNIQUE" not in sql


def test_schema_has_no_foreign_key_to_credentials():
    sql = normalize(RESOURCE_DEFINITIONS_SCHEMA_SQL).upper()
    assert "REFERENCES" not in sql
    assert "FOREIGN KEY" not in sql


def test_schema_is_non_destructive():
    sql = normalize(RESOURCE_DEFINITIONS_SCHEMA_SQL).upper()
    assert "DROP " not in sql
    assert "ALTER " not in sql


# -- initialize(): success path -----------------------------------------------------


async def test_initialize_executes_schema_and_commits(repo, fake_db):
    await repo.initialize()
    connection = fake_db.last_connection
    assert connection.executed == [normalize(RESOURCE_DEFINITIONS_SCHEMA_SQL)]
    assert connection.commit_calls == 1
    assert connection.rollback_calls == 0


async def test_initialize_is_repeatable_and_non_destructive(repo, fake_db):
    await repo.initialize()
    await repo.initialize()
    await repo.initialize()
    # Each call used its own connection and did the full
    # execute schema -> commit -> close cycle, never a rollback,
    # and never any destructive SQL.
    assert len(fake_db.connections) == 3
    for connection in fake_db.connections:
        assert connection.executed == [normalize(RESOURCE_DEFINITIONS_SCHEMA_SQL)]
        assert connection.commit_calls == 1
        assert connection.rollback_calls == 0
        assert connection.closed is True
        for statement in connection.executed:
            assert not any(
                kw in statement.upper() for kw in DESTRUCTIVE_KEYWORDS
            )


async def test_initialize_closes_connection_on_success(repo, fake_db):
    await repo.initialize()
    assert fake_db.last_connection.closed is True


# -- initialize(): failure paths -----------------------------------------------------


async def test_execute_failure_propagates_and_rolls_back(repo, fake_db):
    failure = RuntimeError("connection reset by peer")
    fake_db.next_connection.fail_next_execute = failure
    with pytest.raises(RuntimeError, match="connection reset by peer"):
        await repo.initialize()
    connection = fake_db.last_connection
    assert connection.rollback_calls == 1
    assert connection.commit_calls == 0
    assert connection.closed is True


async def test_commit_failure_propagates_and_rolls_back(repo, fake_db):
    failure = RuntimeError("commit failed")
    fake_db.next_connection.fail_on_commit = failure
    with pytest.raises(RuntimeError, match="commit failed"):
        await repo.initialize()
    connection = fake_db.last_connection
    assert connection.rollback_calls == 1
    assert connection.closed is True


async def test_connection_failure_propagates(repo):
    async def failing_factory() -> Any:
        raise RuntimeError("cannot connect")

    repository = PostgreSQLResourceRepository(failing_factory)
    with pytest.raises(RuntimeError, match="cannot connect"):
        await repository.initialize()


async def test_close_failure_does_not_mask_original_error(repo, fake_db):
    class BrokenCloseConnection(FakeAsyncConnection):
        async def close(self) -> None:
            raise RuntimeError("close exploded")

    fake_db.next_connection = BrokenCloseConnection()
    fake_db.next_connection.fail_next_execute = RuntimeError("original boom")
    with pytest.raises(RuntimeError, match="original boom"):
        await repo.initialize()
    assert fake_db.last_connection.rollback_calls == 1
