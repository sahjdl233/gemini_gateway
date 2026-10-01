"""ResourceRepository contract tests (DB-RESOURCE-001-2).

The suite is written against the :class:`ResourceRepository` contract,
parameterized over implementations.  Today the only implementation is
the in-memory ``ResourceStore``; the future PostgreSQL repository must
satisfy the same contract without provider changes.

Contract under test (frozen semantics):
    add    -> duplicate (provider, id) raises
              DuplicateResourceDefinitionError
    get    -> None for unknown (provider, id)
    require-> UnknownResourceDefinitionError for unknown
    update -> UnknownResourceDefinitionError when missing; full replace
    remove -> idempotent for unknown (provider, id)
    list   -> deterministic ascending (provider, id)
"""

from __future__ import annotations

from typing import List, Optional

import pytest

from core.credential import Credential, CredentialStore, CredentialType
from core.resource_definition import (
    AntigravityResourceDefinition,
    GeminiCliResourceDefinition,
    ResourceDefinitionBase,
)
from core.resource_repository import (
    DuplicateResourceDefinitionError,
    ResourceRepository,
    UnknownResourceDefinitionError,
)


def make_in_memory_repo() -> ResourceRepository:
    return ResourceStore()


IMPLEMENTATIONS = [make_in_memory_repo]


@pytest.fixture(params=IMPLEMENTATIONS, ids=["in-memory"])
def repo(request) -> ResourceRepository:
    return request.param()


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
        enabled=True,
        credential_id=None,
        project_id=project_id,
    )


class ResourceStore(ResourceRepository):
    """Minimal in-memory repository for contract testing."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], ResourceDefinitionBase] = {}

    async def add(self, definition: ResourceDefinitionBase) -> ResourceDefinitionBase:
        key = (definition.provider, definition.id)
        if key in self._store:
            raise DuplicateResourceDefinitionError(
                definition.provider,
                definition.id,
                f"duplicate resource definition: provider={definition.provider!r}, "
                f"id={definition.id!r}",
            )
        self._store[key] = definition
        return definition

    async def get(
        self, provider: str, resource_id: str
    ) -> Optional[ResourceDefinitionBase]:
        return self._store.get((provider, resource_id))

    async def require(
        self, provider: str, resource_id: str
    ) -> ResourceDefinitionBase:
        key = (provider, resource_id)
        if key not in self._store:
            raise UnknownResourceDefinitionError(
                provider,
                resource_id,
                f"resource definition not found: provider={provider!r}, "
                f"id={resource_id!r}",
            )
        return self._store[key]

    async def list(
        self, *, provider: Optional[str] = None
    ) -> List[ResourceDefinitionBase]:
        if provider is not None:
            items = [
                defn
                for (p, rid), defn in self._store.items()
                if p == provider
            ]
        else:
            items = list(self._store.values())
        # Sort by composite key (provider, id)
        items.sort(key=lambda d: (d.provider, d.id))
        return items

    async def update(
        self, definition: ResourceDefinitionBase
    ) -> ResourceDefinitionBase:
        key = (definition.provider, definition.id)
        if key not in self._store:
            raise UnknownResourceDefinitionError(
                definition.provider,
                definition.id,
                f"resource definition not found: provider={definition.provider!r}, "
                f"id={definition.id!r}",
            )
        self._store[key] = definition
        return definition

    async def delete(
        self, provider: str, resource_id: str
    ) -> None:
        self._store.pop((provider, resource_id), None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_and_get(repo: ResourceRepository) -> None:
    """add stores; get retrieves by composite key."""
    defn = antigravity_def()
    saved = await repo.add(defn)
    retrieved = await repo.get("antigravity", "r1")
    assert retrieved is not None
    assert retrieved.provider == "antigravity"
    assert retrieved.id == "r1"


@pytest.mark.asyncio
async def test_add_duplicate_raises(repo: ResourceRepository) -> None:
    """Duplicate (provider, id) raises DuplicateResourceDefinitionError."""
    defn = antigravity_def()
    await repo.add(defn)
    with pytest.raises(DuplicateResourceDefinitionError) as exc:
        await repo.add(defn)
    # The error carries the composite identity as attributes.
    assert exc.value.provider == "antigravity"
    assert exc.value.resource_id == "r1"
    assert "antigravity" in str(exc.value)
    assert "r1" in str(exc.value)


@pytest.mark.asyncio
async def test_get_missing_returns_none(repo: ResourceRepository) -> None:
    """get returns None for unknown composite key."""
    result = await repo.get("antigravity", "nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_require_missing_raises(repo: ResourceRepository) -> None:
    """require raises UnknownResourceDefinitionError for unknown key."""
    with pytest.raises(UnknownResourceDefinitionError) as exc:
        await repo.require("antigravity", "nonexistent")
    # The error carries the composite identity as attributes.
    assert exc.value.provider == "antigravity"
    assert exc.value.resource_id == "nonexistent"
    assert "antigravity" in str(exc.value)
    assert "nonexistent" in str(exc.value)


@pytest.mark.asyncio
async def test_update_missing_raises(repo: ResourceRepository) -> None:
    """update raises for missing definition (not upsert)."""
    new_defn = antigravity_def(rid="r1", project_id="proj-new")
    with pytest.raises(UnknownResourceDefinitionError) as exc:
        await repo.update(new_defn)
    # The error carries the composite identity as attributes.
    assert exc.value.provider == "antigravity"
    assert exc.value.resource_id == "r1"
    assert "r1" in str(exc.value)


@pytest.mark.asyncio
async def test_update_replaces(repo: ResourceRepository) -> None:
    """update fully replaces existing definition."""
    defn = antigravity_def(rid="r1", project_id="proj-original")
    await repo.add(defn)
    updated = antigravity_def(rid="r1", project_id="proj-updated")
    result = await repo.update(updated)
    assert result.project_id == "proj-updated"
    # Verify original is gone
    retrieved = await repo.get("antigravity", "r1")
    assert retrieved is not None
    assert retrieved.project_id == "proj-updated"


@pytest.mark.asyncio
async def test_delete_idempotent(repo: ResourceRepository) -> None:
    """delete is idempotent — deleting non-existent is a no-op."""
    defn = antigravity_def()
    await repo.add(defn)
    await repo.delete("antigravity", "r1")
    # Second delete should not raise
    await repo.delete("antigravity", "r1")
    assert await repo.get("antigravity", "r1") is None


@pytest.mark.asyncio
async def test_list_deterministic_order(repo: ResourceRepository) -> None:
    """list returns items sorted by (provider, id)."""
    defs = [
        antigravity_def(rid="b"),
        antigravity_def(rid="a"),
        antigravity_def(rid="c"),
    ]
    for d in defs:
        await repo.add(d)
    
    items = await repo.list()
    assert [d.id for d in items] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_list_filtered_by_provider(
    repo: ResourceRepository,
) -> None:
    """list with provider filter returns only matching definitions."""
    antigravity_defn = antigravity_def(rid="r1")
    gemini_defn = gemini_def(rid="r2")
    await repo.add(antigravity_defn)
    await repo.add(gemini_defn)
    
    antigravity_items = await repo.list(provider="antigravity")
    assert len(antigravity_items) == 1
    assert antigravity_items[0].provider == "antigravity"
    
    gemini_items = await repo.list(provider="gemini_cli")
    assert len(gemini_items) == 1
    assert gemini_items[0].provider == "gemini_cli"
