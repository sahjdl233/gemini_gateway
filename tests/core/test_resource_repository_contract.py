"""ResourceRepository contract compliance suite (DB-RESOURCE-001-5, Part C).

One shared, frozen-contract test suite, parameterized over every
``ResourceRepository`` implementation:

* ``FakeResourceRepository`` — the in-memory store (alias of the
  ``ResourceStore`` from the DB-RESOURCE-001-2 contract tests);
* ``PostgreSQLResourceRepository`` — over the in-memory async fake
  connection (no real PostgreSQL required).

Every implementation must satisfy the same error semantics:

* ``get``   — missing key returns ``None`` (never raises)
* ``require`` / ``update`` — missing key raises
  ``UnknownResourceDefinitionError`` carrying ``provider`` and
  ``resource_id`` attributes
* ``add``   — duplicate ``(provider, resource_id)`` raises
  ``DuplicateResourceDefinitionError``
* ``update`` — full replacement only, never an upsert
* ``delete`` — idempotent
* ``list``  — deterministic ascending order by the composite key
"""

from __future__ import annotations

from typing import List, Optional

import pytest

from core.resource_definition import (
    AntigravityResourceDefinition,
    FirebaseResourceDefinition,
    GeminiCliResourceDefinition,
    ResourceDefinitionBase,
)
from core.resource_postgres import PostgreSQLResourceRepository
from core.resource_repository import (
    DuplicateResourceDefinitionError,
    ResourceRepository,
    UnknownResourceDefinitionError,
)
from tests.core.test_resource_postgres_crud import (
    FakeAsyncPostgres,
    antigravity_def,
    gemini_def,
)
from tests.core.test_resource_repository import ResourceStore


class FakeResourceRepository(ResourceStore):
    """The in-memory store contract-tested since DB-RESOURCE-001-2,
    re-exported under the name used by this suite."""


def make_fake() -> ResourceRepository:
    return FakeResourceRepository()


def make_postgres() -> ResourceRepository:
    return PostgreSQLResourceRepository(FakeAsyncPostgres().connection_factory())


@pytest.fixture(
    params=[make_fake, make_postgres],
    ids=["fake", "postgres"],
)
def repo(request) -> ResourceRepository:
    return request.param()


# -- add ------------------------------------------------------------------------


async def test_add_then_get_round_trip(repo: ResourceRepository):
    defn = antigravity_def(rid="r1", project_id="proj-1")
    await repo.add(defn)
    result = await repo.get("antigravity", "r1")
    assert result == defn


async def test_add_duplicate_raises_duplicate_error(repo: ResourceRepository):
    await repo.add(antigravity_def(rid="r1"))
    with pytest.raises(DuplicateResourceDefinitionError) as exc:
        await repo.add(antigravity_def(rid="r1", project_id="proj-other"))
    assert exc.value.provider == "antigravity"
    assert exc.value.resource_id == "r1"


async def test_add_same_id_under_different_providers_ok(
    repo: ResourceRepository,
):
    """Identity is the composite key — same id, different providers."""
    await repo.add(antigravity_def(rid="shared"))
    await repo.add(gemini_def(rid="shared"))
    assert await repo.get("antigravity", "shared") is not None
    assert await repo.get("gemini_cli", "shared") is not None


# -- get / require ----------------------------------------------------------------


async def test_get_missing_returns_none(repo: ResourceRepository):
    await repo.add(antigravity_def(rid="r1"))
    assert await repo.get("antigravity", "missing") is None
    assert await repo.get("firebase", "r1") is None


async def test_require_missing_raises_unknown(repo: ResourceRepository):
    with pytest.raises(UnknownResourceDefinitionError) as exc:
        await repo.require("antigravity", "missing")
    assert exc.value.provider == "antigravity"
    assert exc.value.resource_id == "missing"


async def test_require_existing_returns_definition(repo: ResourceRepository):
    defn = gemini_def(rid="r2")
    await repo.add(defn)
    assert await repo.require("gemini_cli", "r2") == defn


# -- list -----------------------------------------------------------------------


async def test_list_all_deterministic_ascending(repo: ResourceRepository):
    for rid in ("c", "a", "b"):
        await repo.add(antigravity_def(rid=rid))
    await repo.add(gemini_def(rid="a"))

    items = await repo.list()
    assert [(d.provider, d.id) for d in items] == [
        ("antigravity", "a"),
        ("antigravity", "b"),
        ("antigravity", "c"),
        ("gemini_cli", "a"),
    ]


async def test_list_provider_filter(repo: ResourceRepository):
    await repo.add(antigravity_def(rid="b"))
    await repo.add(gemini_def(rid="a"))
    await repo.add(antigravity_def(rid="a"))

    items = await repo.list(provider="antigravity")
    assert [d.id for d in items] == ["a", "b"]
    assert all(d.provider == "antigravity" for d in items)


# -- update ----------------------------------------------------------------------


async def test_update_fully_replaces(repo: ResourceRepository):
    await repo.add(antigravity_def(rid="r1", project_id="proj-old"))
    replacement = AntigravityResourceDefinition(
        id="r1", enabled=False, credential_id=None, project_id="proj-new"
    )
    await repo.update(replacement)
    result = await repo.get("antigravity", "r1")
    assert result == replacement


async def test_update_missing_raises_unknown(repo: ResourceRepository):
    with pytest.raises(UnknownResourceDefinitionError) as exc:
        await repo.update(antigravity_def(rid="ghost"))
    assert exc.value.provider == "antigravity"
    assert exc.value.resource_id == "ghost"


# -- delete ----------------------------------------------------------------------


async def test_delete_existing(repo: ResourceRepository):
    await repo.add(antigravity_def(rid="r1"))
    await repo.delete("antigravity", "r1")
    assert await repo.get("antigravity", "r1") is None


async def test_delete_missing_idempotent(repo: ResourceRepository):
    await repo.add(antigravity_def(rid="r1"))
    await repo.delete("antigravity", "nope")
    await repo.delete("antigravity", "r1")
    await repo.delete("antigravity", "r1")
    assert await repo.get("antigravity", "r1") is None


# -- provider-specific field fidelity ----------------------------------------------


async def test_provider_specific_fields_survive(repo: ResourceRepository):
    """Every implementation preserves provider-specific fields, not just
    the common identity columns."""
    defn = GeminiCliResourceDefinition(
        id="g1", enabled=True, credential_id="cred-1",
        project_id="p", tier="paid", pinned_model="gemini-2.5-pro",
        proxy="http://10.0.0.1:8080", preview=False,
    )
    await repo.add(defn)
    assert await repo.get("gemini_cli", "g1") == defn

    fb = FirebaseResourceDefinition(
        id="f1", enabled=False, credential_id=None,
        project_id="fp", pinned_model="gemini-2.0-flash", proxy=None,
    )
    await repo.add(fb)
    assert await repo.get("firebase", "f1") == fb
