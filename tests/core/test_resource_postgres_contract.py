"""ResourceRepository contract verification for the PostgreSQL
implementation (DB-RESOURCE-001-4-FIX).

Runs the frozen DB-RESOURCE-001-2 contract semantics against
:class:`core.resource_postgres.PostgreSQLResourceRepository` using the
in-memory async fake connection from ``test_resource_postgres_crud`` —
no real PostgreSQL required.

Coverage per contract:

* add      — composite (provider, resource_id) key, definition JSONB
             round-trip, duplicate → DuplicateResourceDefinitionError
* get      — existing returns DTO, missing returns None
* require  — missing raises UnknownResourceDefinitionError carrying
             provider and resource_id attributes
* list     — deterministic order: all rows by (provider, resource_id),
             provider filter by resource_id (asserted on the SQL used)
* update   — full replacement; missing → UnknownResourceDefinitionError;
             never falls back to an upsert
* delete   — existing removed; missing key idempotent
* transactions — success: commit + close; driver failure: rollback +
             close; asyncio cancellation (CancelledError, a
             BaseException): rollback + close and the cancellation
             still propagates.

Real-database integration, if added later, must follow the
``GEMINI_GATEWAY_TEST_DATABASE_URL`` opt-in convention.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

import pytest

from core.resource_definition import (
    AntigravityResourceDefinition,
    GeminiCliResourceDefinition,
    FirebaseResourceDefinition,
    resource_definition_from_row,
)
from core.resource_postgres import (
    _LIST_ALL_SQL,
    _LIST_BY_PROVIDER_SQL,
    PostgreSQLResourceRepository,
)
from core.resource_repository import (
    DuplicateResourceDefinitionError,
    UnknownResourceDefinitionError,
)
from tests.core.test_resource_postgres_crud import (
    FakeAsyncPostgres,
    antigravity_def,
    gemini_def,
    normalize,
)


@pytest.fixture
def fake_db() -> FakeAsyncPostgres:
    return FakeAsyncPostgres()


@pytest.fixture
def repo(fake_db: FakeAsyncPostgres) -> PostgreSQLResourceRepository:
    return PostgreSQLResourceRepository(fake_db.connection_factory())


# -- add ------------------------------------------------------------------------


async def test_add_uses_composite_key(repo, fake_db):
    """Same resource_id under different providers coexist."""
    await repo.add(antigravity_def(rid="shared"))
    await repo.add(gemini_def(rid="shared"))
    assert set(fake_db.raw_rows()) == {
        ("antigravity", "shared"),
        ("gemini_cli", "shared"),
    }


async def test_add_definition_jsonb_round_trip(repo, fake_db):
    """Provider-specific fields survive the JSONB write/read cycle."""
    await repo.add(
        GeminiCliResourceDefinition(
            id="r1",
            enabled=True,
            credential_id="cred-1",
            project_id="proj-x",
            tier="paid",
            pinned_model="gemini-2.5-pro",
            preview=False,
        )
    )
    stored = fake_db.raw_rows()[("gemini_cli", "r1")]
    assert json.loads(stored["definition"]) == {
        "project_id": "proj-x",
        "tier": "paid",
        "pinned_model": "gemini-2.5-pro",
        "ide_type": "GCLI",
        "platform": "PLATFORM_UNSPECIFIED",
        "plugin_type": "GEMINI",
        "preview": False,
    }
    result = await repo.get("gemini_cli", "r1")
    assert isinstance(result, GeminiCliResourceDefinition)
    assert result.project_id == "proj-x"
    assert result.tier == "paid"
    assert result.pinned_model == "gemini-2.5-pro"
    assert result.preview is False
    assert result.credential_id == "cred-1"


async def test_jsonb_round_trip_pipeline_direct(fake_db):
    """Explicit DTO ↔ DB pipeline, independent of the repository:

    DTO -> to_definition_json() -> json.dumps() -> DB JSONB column
       -> resource_definition_from_row() -> equal DTO.
    """
    original = GeminiCliResourceDefinition(
        id="r1",
        enabled=True,
        credential_id="cred-1",
        project_id="proj-x",
        tier="paid",
        pinned_model="gemini-2.5-pro",
        proxy=None,
        preview=False,
    )
    # Write side: exactly what the repository puts in the JSONB column.
    body = original.to_definition_json()
    stored = json.dumps(body)
    fake_db.table[("gemini_cli", "r1")] = {
        "provider": "gemini_cli",
        "resource_id": "r1",
        "enabled": original.enabled,
        "credential_id": original.credential_id,
        # As psycopg would return it: JSONB deserialized back to a dict.
        "definition": json.loads(stored),
    }
    # Read side: exactly what the repository's row mapping calls.
    row = fake_db.table[("gemini_cli", "r1")]
    rebuilt = resource_definition_from_row(
        provider=row["provider"],
        resource_id=row["resource_id"],
        enabled=row["enabled"],
        credential_id=row["credential_id"],
        definition=row["definition"],
    )
    assert isinstance(rebuilt, GeminiCliResourceDefinition)
    assert rebuilt == original
    # Provider-specific (non-secret) fields survive; no runtime or secret
    # material exists anywhere in the stored body.
    assert rebuilt.project_id == "proj-x"
    assert rebuilt.tier == "paid"
    assert rebuilt.pinned_model == "gemini-2.5-pro"
    assert rebuilt.preview is False


async def test_add_duplicate_raises_duplicate_error(repo, fake_db):
    await repo.add(antigravity_def(rid="r1"))
    with pytest.raises(DuplicateResourceDefinitionError) as exc:
        await repo.add(antigravity_def(rid="r1", project_id="proj-other"))
    assert exc.value.provider == "antigravity"
    assert exc.value.resource_id == "r1"
    # The original row is untouched by the rejected write.
    stored = fake_db.raw_rows()[("antigravity", "r1")]
    assert json.loads(stored["definition"])["project_id"] == "proj-1"


# -- get / require ----------------------------------------------------------------


async def test_get_existing_returns_dto(repo, fake_db):
    await repo.add(antigravity_def(rid="r1"))
    result = await repo.get("antigravity", "r1")
    assert isinstance(result, AntigravityResourceDefinition)
    assert result.provider == "antigravity"
    assert result.id == "r1"
    assert result.enabled is True
    assert result.credential_id == "cred-1"


async def test_get_missing_returns_none(repo, fake_db):
    await repo.add(antigravity_def(rid="r1"))
    assert await repo.get("antigravity", "missing") is None
    assert await repo.get("firebase", "r1") is None


async def test_require_missing_raises_with_identity(repo, fake_db):
    with pytest.raises(UnknownResourceDefinitionError) as exc:
        await repo.require("antigravity", "missing")
    assert exc.value.provider == "antigravity"
    assert exc.value.resource_id == "missing"
    assert "antigravity" in str(exc.value)
    assert "missing" in str(exc.value)


# -- list -----------------------------------------------------------------------


async def test_list_all_ordered_by_provider_then_id(repo, fake_db):
    await repo.add(antigravity_def(rid="b"))
    await repo.add(gemini_def(rid="a"))
    await repo.add(antigravity_def(rid="a"))
    await repo.add(FirebaseResourceDefinition(id="z", enabled=True))

    items = await repo.list()
    assert [(d.provider, d.id) for d in items] == [
        ("antigravity", "a"),
        ("antigravity", "b"),
        ("firebase", "z"),
        ("gemini_cli", "a"),
    ]
    # Ordering is decided by the statement itself.
    assert fake_db.last_connection.executed == [normalize(_LIST_ALL_SQL)]


async def test_list_provider_filter_ordered_by_id(repo, fake_db):
    await repo.add(antigravity_def(rid="c"))
    await repo.add(antigravity_def(rid="a"))
    await repo.add(gemini_def(rid="a"))

    items = await repo.list(provider="antigravity")
    assert [d.id for d in items] == ["a", "c"]
    assert all(d.provider == "antigravity" for d in items)
    assert fake_db.last_connection.executed == [
        normalize(_LIST_BY_PROVIDER_SQL)
    ]


# -- update ----------------------------------------------------------------------


async def test_update_fully_replaces(repo, fake_db):
    await repo.add(antigravity_def(rid="r1", project_id="proj-old"))
    await repo.update(
        AntigravityResourceDefinition(
            id="r1",
            enabled=False,
            credential_id=None,
            project_id="proj-new",
        )
    )
    stored = fake_db.raw_rows()[("antigravity", "r1")]
    assert stored["enabled"] is False
    assert stored["credential_id"] is None
    assert json.loads(stored["definition"]) == {
        "project_id": "proj-new",
        "ide_type": "ANTIGRAVITY",
    }
    result = await repo.get("antigravity", "r1")
    assert result.enabled is False
    assert result.credential_id is None
    assert result.project_id == "proj-new"


async def test_update_missing_raises_not_upsert(repo, fake_db):
    with pytest.raises(UnknownResourceDefinitionError) as exc:
        await repo.update(antigravity_def(rid="ghost"))
    assert exc.value.provider == "antigravity"
    assert exc.value.resource_id == "ghost"
    # No upsert fallback: the table stays empty.
    assert fake_db.raw_rows() == {}
    assert fake_db.last_connection.rollback_calls == 1
    assert fake_db.last_connection.commit_calls == 0


# -- delete ----------------------------------------------------------------------


async def test_delete_existing_removes(repo, fake_db):
    await repo.add(antigravity_def(rid="r1"))
    await repo.delete("antigravity", "r1")
    assert fake_db.raw_rows() == {}
    assert await repo.get("antigravity", "r1") is None


async def test_delete_missing_is_idempotent(repo, fake_db):
    await repo.add(antigravity_def(rid="r1"))
    await repo.delete("antigravity", "nope")
    await repo.delete("antigravity", "r1")
    await repo.delete("antigravity", "r1")
    assert await repo.get("antigravity", "r1") is None
    assert fake_db.last_connection.commit_calls == 1
    assert fake_db.last_connection.rollback_calls == 0


# -- transaction lifecycle ---------------------------------------------------------


async def test_success_commits_and_closes(repo, fake_db):
    await repo.add(antigravity_def(rid="r1"))
    connection = fake_db.last_connection
    assert connection.commit_calls == 1
    assert connection.rollback_calls == 0
    assert connection.closed is True


async def test_driver_failure_rolls_back_and_closes(repo, fake_db):
    fake_db.next_connection.fail_next_execute = RuntimeError("db down")
    with pytest.raises(RuntimeError, match="db down"):
        await repo.add(antigravity_def(rid="r1"))
    connection = fake_db.last_connection
    assert connection.rollback_calls == 1
    assert connection.commit_calls == 0
    assert connection.closed is True
    assert fake_db.raw_rows() == {}


async def test_cancellation_rolls_back_and_closes(repo, fake_db):
    """CancelledError is a BaseException — the transaction must still be
    rolled back and the connection closed, and the cancellation must
    propagate (not be swallowed or translated)."""
    fake_db.next_connection.fail_next_execute = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await repo.add(antigravity_def(rid="r1"))
    connection = fake_db.last_connection
    assert connection.rollback_calls == 1
    assert connection.commit_calls == 0
    assert connection.closed is True
    assert fake_db.raw_rows() == {}


async def test_cancellation_during_commit_rolls_back_and_closes(
    repo, fake_db
):
    fake_db.next_connection.fail_on_commit = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await repo.add(antigravity_def(rid="r1"))
    connection = fake_db.last_connection
    assert connection.rollback_calls == 1
    assert connection.closed is True
    assert fake_db.raw_rows() == {}


async def test_initialize_cancellation_rolls_back_and_closes(repo, fake_db):
    fake_db.next_connection.fail_next_execute = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await repo.initialize()
    connection = fake_db.last_connection
    assert connection.rollback_calls == 1
    assert connection.closed is True
