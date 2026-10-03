"""ResourceDefinition repository abstraction tests (DB-RESOURCE-004).

Verifies the read-side boundary is stable:

* the in-memory repository satisfies the ``ResourceDefinitionRepository``
  Protocol (structural typing — no inheritance required);
* duplicate composite identities fail eagerly and explicitly;
* the provider discriminator travels with every returned DTO;
* the config adapter is indistinguishable from a hand-seeded memory
  repository through the Protocol;
* the bootstrap service consumes DTOs regardless of source.
"""

from __future__ import annotations

import pytest

from core.resource_bootstrap import BootstrapMode, ResourceBootstrapService
from core.resource_definition import (
    AntigravityResourceDefinition,
    GeminiCliResourceDefinition,
)
from core.resource_definition_loader import ConfigResourceDefinitionRepository
from core.resource_definition_repository import (
    MemoryResourceDefinitionRepository,
    ResourceDefinitionRepository,
    ResourceRepositoryDefinitionSource,
)
from core.resource_postgres import PostgreSQLResourceRepository
from core.resource_repository import DuplicateResourceDefinitionError
from tests.core.test_resource_postgres_crud import FakeAsyncPostgres


def antigravity(rid: str = "r1", project_id: str = "p1"):
    return AntigravityResourceDefinition(
        id=rid, enabled=True, credential_id="cred-1", project_id=project_id
    )


def gemini(rid: str = "g1", tier: str = "paid"):
    return GeminiCliResourceDefinition(
        id=rid, enabled=False, credential_id=None, project_id="p",
        tier=tier,
    )


def test_memory_repository_satisfies_protocol():
    repo: ResourceDefinitionRepository = MemoryResourceDefinitionRepository(
        [antigravity()]
    )
    assert isinstance(repo, MemoryResourceDefinitionRepository)


async def test_list_definitions_sorted_and_complete():
    repo = MemoryResourceDefinitionRepository(
        [gemini(rid="z"), antigravity(rid="b"), antigravity(rid="a")]
    )
    defs = await repo.list_definitions()
    assert [(d.provider, d.id) for d in defs] == [
        ("antigravity", "a"),
        ("antigravity", "b"),
        ("gemini_cli", "z"),
    ]


async def test_get_definition_hit_and_miss():
    repo = MemoryResourceDefinitionRepository([antigravity(rid="r1")])
    found = await repo.get_definition("antigravity", "r1")
    assert found is not None
    assert found.provider == "antigravity"
    assert found.id == "r1"
    assert await repo.get_definition("antigravity", "nope") is None
    # Same resource_id under another provider misses: composite identity.
    assert await repo.get_definition("gemini_cli", "r1") is None


async def test_duplicate_identity_rejected_eagerly():
    with pytest.raises(DuplicateResourceDefinitionError) as exc:
        MemoryResourceDefinitionRepository(
            [antigravity(rid="r1"), antigravity(rid="r1", project_id="p2")]
        )
    assert exc.value.provider == "antigravity"
    assert exc.value.resource_id == "r1"
    # Explicit policy: not last-one-wins, nothing was stored.
    repo = MemoryResourceDefinitionRepository([])
    assert await repo.list_definitions() == []


async def test_provider_discriminator_preserved():
    repo = MemoryResourceDefinitionRepository([gemini(rid="g1", tier="paid")])
    found = await repo.get_definition("gemini_cli", "g1")
    # The DTO keeps its concrete provider type and provider-specific
    # fields — the read boundary does not flatten to a generic shape.
    assert type(found) is GeminiCliResourceDefinition
    assert found.tier == "paid"
    assert found.provider == "gemini_cli"


# -- loader as a repository implementation --------------------------------------------


def make_config():
    return {
        "providers": {
            "gemini_cli": {"resources": [{"id": "z"}, {"id": "a"}]},
            "antigravity": {
                "resources": [{"id": "b", "credential_id": "cred-9"}]
            },
        }
    }


async def test_config_adapter_equivalent_to_memory_repository():
    via_config: ResourceDefinitionRepository = (
        ConfigResourceDefinitionRepository(make_config())
    )
    via_seed: ResourceDefinitionRepository = MemoryResourceDefinitionRepository(
        [
            AntigravityResourceDefinition(
                id="b", enabled=True, credential_id="cred-9", project_id=None
            ),
            GeminiCliResourceDefinition(id="a"),
            GeminiCliResourceDefinition(id="z"),
        ]
    )
    assert await via_config.list_definitions() == (
        await via_seed.list_definitions()
    )
    assert await via_config.get_definition("gemini_cli", "a") == (
        await via_seed.get_definition("gemini_cli", "a")
    )


async def test_config_adapter_rejects_duplicate_identity():
    config = {
        "providers": {
            "antigravity": {"resources": [
                {"id": "r1", "project_id": "p1"},
                {"id": "r1", "project_id": "p2"},
            ]}
        }
    }
    # The loader passes duplicates through; the repository layer is where
    # the policy bites.  The config adapter materializes lazily, so the
    # rejection fires on first access, not on construction.
    repo = ConfigResourceDefinitionRepository(config)
    with pytest.raises(DuplicateResourceDefinitionError):
        await repo.list_definitions()


async def test_config_adapter_preserves_discriminator():
    repo = ConfigResourceDefinitionRepository(make_config())
    found = await repo.get_definition("antigravity", "b")
    assert type(found) is AntigravityResourceDefinition
    assert found.credential_id == "cred-9"


# -- bootstrap boundary ----------------------------------------------------------------


async def test_bootstrap_consumes_dtos_regardless_of_source():
    """The same bootstrap CHECK plan results whether the definitions came
    from the config adapter or were constructed directly — the service
    sees DTOs, never a source."""
    config_defs = await ConfigResourceDefinitionRepository(
        make_config()
    ).list_definitions()
    direct_defs = [
        antigravity(rid="b", project_id=None),
        gemini(rid="a", tier="unknown"),
        gemini(rid="z", tier="unknown"),
    ]
    # Align credential_id/fields between the two sources.
    direct_defs[0].credential_id = "cred-9"

    results = []
    for defs in (config_defs, direct_defs):
        repo = PostgreSQLResourceRepository(
            FakeAsyncPostgres().connection_factory()
        )
        results.append(
            await ResourceBootstrapService(
                ResourceRepositoryDefinitionSource(repo)
            ).run(defs, BootstrapMode.CHECK)
        )
    assert [r.key for r in results[0].added] == [r.key for r in results[1].added]
    assert [(r.provider, r.resource_id) for r in results[0].added] == [
        ("antigravity", "b"),
        ("gemini_cli", "a"),
        ("gemini_cli", "z"),
    ]


async def test_bootstrap_plan_uses_memory_repository_definitions():
    """End-to-end read boundary: memory repository → DTOs → bootstrap
    IMPORT writes them to the (fake) durable store unchanged."""
    repo = MemoryResourceDefinitionRepository(
        [antigravity(rid="r1"), gemini(rid="g1")]
    )
    durable = PostgreSQLResourceRepository(
        FakeAsyncPostgres().connection_factory()
    )
    service = ResourceBootstrapService.over_repository(durable)
    result = await service.run(
        await repo.list_definitions(), BootstrapMode.IMPORT
    )
    assert [r.key for r in result.added] == [
        ("antigravity", "r1"),
        ("gemini_cli", "g1"),
    ]
    # And the durable store reads back through the same abstraction.
    assert await durable.get("antigravity", "r1") == (
        await repo.get_definition("antigravity", "r1")
    )
