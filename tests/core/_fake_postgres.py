"""Fake PostgreSQL connection for repository unit tests (NOT collected).

Implements the duck-typed connection surface the
``PostgreSQLCredentialRepository`` relies on (psycopg-3 style:
``execute``/``commit``/``rollback``/``close``, cursor ``fetchone`` /
``fetchall`` / ``rowcount``, dict rows) over an in-memory table, WITH
transactional semantics: writes land in a per-connection overlay and are
applied to the shared table only on ``commit()``; ``rollback()``
discards them.

Test double only — it does NOT verify SQL against a real PostgreSQL
server.  Real-database integration is opt-in via
``GEMINI_GATEWAY_TEST_DATABASE_URL`` (see test_credential_postgres.py).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core.credential_postgres import (
    _DELETE_CREDENTIAL_SQL,
    _INSERT_CREDENTIAL_SQL,
    _LIST_CREDENTIALS_SQL,
    _LIST_FOR_UPDATE_SQL,
    _ROTATE_PAYLOAD_SQL,
    _SELECT_BY_ID_SQL,
    _SELECT_TYPE_FOR_UPDATE_SQL,
    _UPDATE_PAYLOAD_RETURNING_SQL,
    CREDENTIALS_SCHEMA_SQL,
)


class FakeUniqueViolation(Exception):
    """Stand-in for a driver unique-violation error (DBAPI SQLSTATE)."""

    sqlstate = "23505"


class FakeCursor:
    def __init__(self, rows: List[Dict[str, Any]], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self) -> Optional[Dict[str, Any]]:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> List[Dict[str, Any]]:
        return list(self._rows)


class FakePostgresConnection:
    """One transaction over the shared ``credentials`` table.

    Writes are buffered in ``self._pending`` (id -> row or None for
    delete) and only merged into the shared table on ``commit()``.
    Reads see committed state overlaid with this transaction's own
    writes (read-your-own-writes), emulating snapshot isolation closely
    enough for the repository's access pattern.
    """

    def __init__(self, table: Dict[str, Dict[str, Any]]) -> None:
        self._table = table
        self._pending: Dict[str, Optional[Dict[str, Any]]] = {}
        self.executed: List[str] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.closed = False
        self.fail_next_execute: Optional[Exception] = None

    # -- row visibility ------------------------------------------------------

    def _row(self, credential_id: str) -> Optional[Dict[str, Any]]:
        """Raw row visible to THIS transaction (pending overlay applied)."""
        if credential_id in self._pending:
            return self._pending[credential_id]
        return self._table.get(credential_id)

    def _as_jsonb(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Emulate a JSONB column: drivers deserialize to a dict on read."""
        row = dict(row)
        if isinstance(row.get("payload_encrypted"), str):
            row["payload_encrypted"] = json.loads(row["payload_encrypted"])
        return row

    # -- statement dispatch -----------------------------------------------------

    def execute(self, sql: str, params: Optional[tuple] = None) -> FakeCursor:
        self.executed.append(" ".join(sql.split()))
        if self.fail_next_execute is not None:
            exc = self.fail_next_execute
            self.fail_next_execute = None
            raise exc

        statement = " ".join(sql.split())
        if statement == " ".join(CREDENTIALS_SCHEMA_SQL.split()):
            return FakeCursor([], 0)
        if statement == " ".join(_INSERT_CREDENTIAL_SQL.split()):
            credential_id, ctype, envelope, created_at, updated_at = params
            if self._row(credential_id) is not None:
                raise FakeUniqueViolation(
                    "duplicate key value violates unique constraint"
                )
            self._pending[credential_id] = {
                "id": credential_id,
                "type": ctype,
                "payload_encrypted": envelope,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            return FakeCursor([], 1)
        if statement == " ".join(_SELECT_BY_ID_SQL.split()):
            (credential_id,) = params
            row = self._row(credential_id)
            return FakeCursor(
                [self._as_jsonb(row)] if row else [], 1 if row else 0
            )
        if statement == " ".join(_SELECT_TYPE_FOR_UPDATE_SQL.split()):
            # Row lock emulation: real PostgreSQL would block concurrent
            # deletes until this transaction ends.
            (credential_id,) = params
            row = self._row(credential_id)
            return FakeCursor(
                [{"type": row["type"]}] if row else [], 1 if row else 0
            )
        if statement == " ".join(_UPDATE_PAYLOAD_RETURNING_SQL.split()):
            envelope, updated_at, credential_id = params
            row = self._row(credential_id)
            if row is None:
                return FakeCursor([], 0)
            row = dict(row)
            row["payload_encrypted"] = envelope
            row["updated_at"] = updated_at
            self._pending[credential_id] = row
            return FakeCursor([self._as_jsonb(row)], 1)
        if statement == " ".join(_ROTATE_PAYLOAD_SQL.split()):
            envelope, credential_id = params
            row = self._row(credential_id)
            if row is None:
                return FakeCursor([], 0)
            row = dict(row)
            row["payload_encrypted"] = envelope
            self._pending[credential_id] = row
            return FakeCursor([self._as_jsonb(row)], 1)
        if statement == " ".join(_DELETE_CREDENTIAL_SQL.split()):
            (credential_id,) = params
            if self._row(credential_id) is None:
                return FakeCursor([], 0)
            self._pending[credential_id] = None  # tombstone
            return FakeCursor([], 1)
        if statement == " ".join(_LIST_CREDENTIALS_SQL.split()) or (
            statement == " ".join(_LIST_FOR_UPDATE_SQL.split())
        ):
            # Emulate ORDER BY created_at, id over the transaction view.
            visible: Dict[str, Dict[str, Any]] = dict(self._table)
            for credential_id, row in self._pending.items():
                if row is None:
                    visible.pop(credential_id, None)
                else:
                    visible[credential_id] = row
            rows = sorted(
                visible.values(), key=lambda r: (r["created_at"], r["id"])
            )
            return FakeCursor(
                [self._as_jsonb(r) for r in rows], len(rows)
            )
        raise AssertionError(f"fake driver received unexpected SQL: {statement}")

    # -- transaction control -------------------------------------------------------

    def commit(self) -> None:
        for credential_id, row in self._pending.items():
            if row is None:
                self._table.pop(credential_id, None)
            else:
                self._table[credential_id] = row
        self._pending.clear()
        self.commit_calls += 1

    def rollback(self) -> None:
        self._pending.clear()
        self.rollback_calls += 1

    def close(self) -> None:
        self.closed = True


class FakePostgres:
    """Test harness: one shared table, fresh connections per operation."""

    def __init__(self) -> None:
        self.table: Dict[str, Dict[str, Any]] = {}
        self.connections: List[FakePostgresConnection] = []

    def connection_factory(self):
        def factory() -> FakePostgresConnection:
            connection = FakePostgresConnection(self.table)
            self.connections.append(connection)
            return connection

        return factory

    def raw_rows(self) -> Dict[str, Dict[str, Any]]:
        """Direct view of the COMMITTED table (bypasses the repository)."""
        return self.table
