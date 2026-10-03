"""Resource bootstrap service core tests (DB-RESOURCE-003-1).

Runs :class:`core.resource_bootstrap.ResourceBootstrapService` against
the real ``PostgreSQLResourceRepository`` over the in-memory async fake
connection (no real PostgreSQL, no YAML, no startup wiring), covering
CHECK / IMPORT / OVERWRITE semantics, fail-closed plan validation,
deterministic ordering, canonical comparison, and repository-error
propagation.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from core.resource_bootstrap import (
    BootstrapMode,
    BootstrapRecord,
    ResourceBootstrapConflictError,
    ResourceBootstrapError,
    ResourceBootstrapService,
    canonical_payload,
)
from core.resource_definition import (
    AnonymousVertexResourceDefinition,
    AntigravityResourceDefinition,
    FakeResourceDefinition,
    FirebaseResourceDefinition,
    GeminiCliResourceDefinition,
)
from core.resource_postgres import PostgreSQLResourceRepository
from core.resource_repository import (
    DuplicateResourceDefinitionError,
    UnknownResourceDefinitionError,
)
from tests.core.test_resource_postgres_crud import (
    FakeAsyncPostgres,
    antigravity_def,
    gemini_def,
)


@pytest.fixture
def fake_db() -> FakeAsyncPostgres:
    return FakeAsyncPostgres()


@pytest.fixture
def repo(fake_db: FakeAsyncPostgres) -> PostgreSQLResourceRepository:
    return PostgreSQLResourceRepository(fake_db.connection_factory())


@pytest.fixture
def service(repo: PostgreSQLResourceRepository) -> ResourceBootstrapService:
    return ResourceBootstrapService.over_repository(repo)


# -- CHECK mode -----------------------------------------------------------------


async def test_check_identical_reports_unchanged(service, fake_db):
    await service.run([antigravity_def(rid="r1")], BootstrapMode.IMPORT)
    result = await service.run([antigravity_def(rid="r1")], BootstrapMode.CHECK)
    assert [r.key for r in result.added] == []
    assert [r.key for r in result.unchanged] == [("antigravity", "r1")]
    assert result.conflicts == []
    assert not result.has_conflicts


async def test_check_new_key_reports_added_candidate(service, fake_db):
    await service.run([antigravity_def(rid="r1")], BootstrapMode.IMPORT)
    result = await service.run(
        [antigravity_def(rid="r1"), gemini_def(rid="new")], BootstrapMode.CHECK
    )
    assert [r.key for r in result.added] == [("gemini_cli", "new")]
    assert [r.key for r in result.unchanged] == [("antigravity", "r1")]
    # CHECK never writes: the candidate is reported, not inserted.
    assert ("gemini_cli", "new") not in fake_db.raw_rows()


async def test_check_difference_reports_conflict_with_payloads(service, fake_db):
    await service.run([antigravity_def(rid="r1", project_id="proj-old")],
                      BootstrapMode.IMPORT)
    result = await service.run(
        [antigravity_def(rid="r1", project_id="proj-new")], BootstrapMode.CHECK
    )
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.provider == "antigravity"
    assert conflict.resource_id == "r1"
    # Structured payloads, not strings.
    assert conflict.existing["definition"] == {
        "project_id": "proj-old", "ide_type": "ANTIGRAVITY",
    }
    assert conflict.incoming["definition"] == {
        "project_id": "proj-new", "ide_type": "ANTIGRAVITY",
    }
    # CHECK wrote nothing: DB still has the old definition.
    stored = fake_db.raw_rows()[("antigravity", "r1")]
    assert json.loads(stored["definition"])["project_id"] == "proj-old"


async def test_check_db_only_preserved(service, fake_db):
    await service.run(
        [antigravity_def(rid="r1"), gemini_def(rid="r2")], BootstrapMode.IMPORT
    )
    result = await service.run([antigravity_def(rid="r1")], BootstrapMode.CHECK)
    assert [r.key for r in result.db_only] == [("gemini_cli", "r2")]
    # db_only means kept, never deleted.
    assert ("gemini_cli", "r2") in fake_db.raw_rows()


# -- IMPORT mode ----------------------------------------------------------------


async def test_import_inserts_missing(service, fake_db):
    result = await service.run([antigravity_def(rid="r1")], BootstrapMode.IMPORT)
    assert [r.key for r in result.added] == [("antigravity", "r1")]
    assert ("antigravity", "r1") in fake_db.raw_rows()
    stored = fake_db.raw_rows()[("antigravity", "r1")]
    assert json.loads(stored["definition"]) == {
        "project_id": "proj-1", "ide_type": "ANTIGRAVITY",
    }


async def test_import_identical_is_noop(service, fake_db):
    await service.run([antigravity_def(rid="r1")], BootstrapMode.IMPORT)
    result = await service.run([antigravity_def(rid="r1")], BootstrapMode.IMPORT)
    assert [r.key for r in result.unchanged] == [("antigravity", "r1")]
    assert [r.key for r in result.added] == []
    # Second run issued no writes beyond the DB read.
    last = fake_db.last_connection
    assert all(
        stmt.split()[0] == "SELECT" for stmt in last.executed
    )


async def test_import_never_overwrites_conflict(service, fake_db):
    await service.run([antigravity_def(rid="r1", project_id="proj-old")],
                      BootstrapMode.IMPORT)
    result = await service.run(
        [antigravity_def(rid="r1", project_id="proj-new")], BootstrapMode.IMPORT
    )
    assert [c.key for c in result.conflicts] == [("antigravity", "r1")]
    assert [r.key for r in result.added] == []
    # DB row untouched.
    stored = fake_db.raw_rows()[("antigravity", "r1")]
    assert json.loads(stored["definition"])["project_id"] == "proj-old"


async def test_import_conflicts_raise_on_demand(service):
    await service.run([antigravity_def(rid="r1")], BootstrapMode.IMPORT)
    result = await service.run(
        [antigravity_def(rid="r1", project_id="proj-new")], BootstrapMode.IMPORT
    )
    with pytest.raises(ResourceBootstrapConflictError) as exc:
        result.raise_if_conflicts()
    assert exc.value.conflicts[0].resource_id == "r1"


# -- OVERWRITE mode ---------------------------------------------------------------


async def test_overwrite_inserts_missing_and_keeps_identical(service, fake_db):
    await service.run([antigravity_def(rid="r1")], BootstrapMode.IMPORT)
    result = await service.run(
        [antigravity_def(rid="r1"), gemini_def(rid="new")],
        BootstrapMode.OVERWRITE,
    )
    assert [r.key for r in result.added] == [("gemini_cli", "new")]
    assert [r.key for r in result.unchanged] == [("antigravity", "r1")]
    assert [r.key for r in result.conflicts] == []
    assert ("gemini_cli", "new") in fake_db.raw_rows()


async def test_overwrite_updates_changed_definition(service, fake_db):
    await service.run([antigravity_def(rid="r1", project_id="proj-old")],
                      BootstrapMode.IMPORT)
    result = await service.run(
        [AntigravityResourceDefinition(
            id="r1", enabled=False, credential_id="cred-9",
            project_id="proj-new",
        )],
        BootstrapMode.OVERWRITE,
    )
    assert [c.key for c in result.conflicts] == [("antigravity", "r1")]
    stored = fake_db.raw_rows()[("antigravity", "r1")]
    assert stored["enabled"] is False
    assert stored["credential_id"] == "cred-9"
    assert json.loads(stored["definition"]) == {
        "project_id": "proj-new", "ide_type": "ANTIGRAVITY",
    }


# -- plan-stage validation ----------------------------------------------------------


async def test_duplicate_incoming_identity_rejected_before_writes(
    service, fake_db,
):
    with pytest.raises(ResourceBootstrapError) as exc:
        await service.run(
            [
                GeminiCliResourceDefinition(id="r1", project_id="a"),
                GeminiCliResourceDefinition(id="r1", project_id="b"),
            ],
            BootstrapMode.IMPORT,
        )
    assert "(gemini_cli, r1)" in str(exc.value)
    # Nothing written.
    assert fake_db.raw_rows() == {}


async def test_non_dto_input_rejected(service):
    with pytest.raises(ResourceBootstrapError, match="ResourceDefinitionBase"):
        await service.run([{"provider": "fake", "id": "x"}], BootstrapMode.CHECK)


async def test_invalid_mode_rejected(service):
    with pytest.raises(ResourceBootstrapError, match="BootstrapMode"):
        await service.run([antigravity_def(rid="r1")], "import")


# -- deterministic ordering ---------------------------------------------------------


async def test_plan_and_result_sorted_by_composite_key(service, fake_db):
    # Insert deliberately out of order.
    await service.run(
        [
            gemini_def(rid="z"),
            antigravity_def(rid="c"),
            antigravity_def(rid="a"),
        ],
        BootstrapMode.IMPORT,
    )
    result = await service.run(
        [
            antigravity_def(rid="b"),
            FakeResourceDefinition(id="k1"),
            gemini_def(rid="a"),
        ],
        BootstrapMode.OVERWRITE,
    )
    assert [r.key for r in result.added] == [
        ("antigravity", "b"),
        ("fake", "k1"),
        ("gemini_cli", "a"),
    ]
    # None of the incoming keys exists in the DB yet — the three earlier
    # rows have different composite keys and are reported db_only (kept).
    assert [r.key for r in result.unchanged] == []
    assert [r.key for r in result.db_only] == [
        ("antigravity", "a"),
        ("antigravity", "c"),
        ("gemini_cli", "z"),
    ]


# -- canonical comparison -----------------------------------------------------------


def test_canonical_payload_stable_and_order_insensitive():
    a = GeminiCliResourceDefinition(
        id="r1", enabled=True, credential_id="c", project_id="p",
        tier="paid", preview=False,
    )
    b = GeminiCliResourceDefinition(
        preview=False, tier="paid", project_id="p",
        credential_id="c", enabled=True, id="r1",
    )
    # Different construction order — identical canonical payload.
    assert canonical_payload(a) == canonical_payload(b)
    assert list(canonical_payload(a)) == [
        "provider", "resource_id", "enabled", "credential_id", "definition",
    ]


def test_canonical_payload_mirrors_persistence_semantics():
    defn = antigravity_def(rid="r1")
    payload = canonical_payload(defn)
    assert payload == {
        "provider": "antigravity",
        "resource_id": "r1",
        "enabled": True,
        "credential_id": "cred-1",
        "definition": defn.to_definition_json(),
    }


# -- all providers enter the plan ----------------------------------------------------


async def test_all_providers_bootstrap_through(
    service, repo, fake_db,
):
    definitions = [
        antigravity_def(rid="a1"),
        gemini_def(rid="g1"),
        FirebaseResourceDefinition(
            id="f1", enabled=True, credential_id="cred-f",
            project_id="p", proxy="socks5://10.0.0.1:1080",
        ),
        AnonymousVertexResourceDefinition(
            id="v1", enabled=False, credential_id=None,
            proxy_scheme="socks5", proxy_host="10.0.0.2", proxy_port=1080,
        ),
        FakeResourceDefinition(
            id="k1", enabled=True, scenario="failure",
            retry_after=2.0, model_ids=["m1"],
        ),
    ]
    result = await service.run(definitions, BootstrapMode.IMPORT)
    assert [r.key for r in result.added] == [
        ("anonymous_vertex", "v1"),
        ("antigravity", "a1"),
        ("fake", "k1"),
        ("firebase", "f1"),
        ("gemini_cli", "g1"),
    ]
    assert set(fake_db.raw_rows()) == {
        ("antigravity", "a1"),
        ("anonymous_vertex", "v1"),
        ("fake", "k1"),
        ("firebase", "f1"),
        ("gemini_cli", "g1"),
    }
    # Round-trip: bootstrap-persisted rows read back as equal DTOs.
    for original in definitions:
        stored_key = (original.provider, original.id)
        row = fake_db.raw_rows()[stored_key]
        rebuilt = await repo.get(*stored_key)
        assert rebuilt == original, stored_key
        assert json.loads(row["definition"]) == original.to_definition_json()


# -- repository errors propagate ------------------------------------------------------


async def test_duplicate_race_error_propagates_unchanged(
    service, repo, fake_db,
):
    """A row inserted after the plan was computed surfaces the
    repository's own DuplicateResourceDefinitionError — never translated,
    never swallowed."""

    class RacyRepository:
        def __init__(self, inner):
            self._inner = inner

        async def add(self, definition):
            # Simulate another writer winning the race between the
            # bootstrap's read and its write.
            await self._inner.add(definition)
            raise DuplicateResourceDefinitionError(
                definition.provider,
                definition.id,
                "injected race",
            )

        def __getattr__(self, name):
            return getattr(self._inner, name)

    # Reads go through the read-only source; the race fires on the sink.
    from core.resource_definition_repository import (
        ResourceRepositoryDefinitionSource,
    )

    racy = ResourceBootstrapService(
        ResourceRepositoryDefinitionSource(repo),
        sink=RacyRepository(repo),
    )
    with pytest.raises(DuplicateResourceDefinitionError):
        await racy.run([antigravity_def(rid="r1")], BootstrapMode.IMPORT)


async def test_unknown_error_from_update_propagates(
    service, repo, fake_db,
):
    class VanishingRepository:
        def __init__(self, inner):
            self._inner = inner

        async def update(self, definition):
            raise UnknownResourceDefinitionError(
                definition.provider, definition.id, "vanished mid-plan"
            )

        def __getattr__(self, name):
            return getattr(self._inner, name)

    await service.run([antigravity_def(rid="r1")], BootstrapMode.IMPORT)
    from core.resource_definition_repository import (
        ResourceRepositoryDefinitionSource,
    )

    vanishing = ResourceBootstrapService(
        ResourceRepositoryDefinitionSource(repo),
        sink=VanishingRepository(repo),
    )
    with pytest.raises(UnknownResourceDefinitionError):
        await vanishing.run(
            [antigravity_def(rid="r1", project_id="proj-new")],
            BootstrapMode.OVERWRITE,
        )


async def test_repository_read_failure_propagates(service, fake_db):
    fake_db.next_connection.fail_next_execute = RuntimeError("db down")
    with pytest.raises(RuntimeError, match="db down"):
        await service.run([antigravity_def(rid="r1")], BootstrapMode.CHECK)


# -- per-operation atomicity note ------------------------------------------------------


async def test_result_notes_disclose_atomicity_limits(service):
    for mode in BootstrapMode:
        result = await service.run([antigravity_def(rid=f"r-{mode.value}")], mode)
        assert any(
            "per-operation atomicity" in note for note in result.notes
        ), mode


# -- 005: source injection via ResourceDefinitionRepository --------------------------


class FakeReadOnlySource:
    """Minimal fake: implements ONLY the read-only 004 Protocol.  The
    bootstrap service must not reach for anything else when planning."""

    def __init__(self, definitions):
        self._definitions = {
            (d.provider, d.id): d for d in definitions
        }

    async def list_definitions(self):
        return [
            self._definitions[key] for key in sorted(self._definitions)
        ]

    async def get_definition(self, provider, id):
        return self._definitions.get((provider, id))


async def test_plan_against_fake_read_only_source():
    """FakeRepository → BootstrapService → plan: the service diffs against
    any ResourceDefinitionRepository without knowing Memory/PG/YAML."""
    source = FakeReadOnlySource(
        [antigravity_def(rid="existing"), gemini_def(rid="db-only")]
    )
    service = ResourceBootstrapService(source)  # no sink — plan only
    result = await service.run(
        [antigravity_def(rid="existing"), antigravity_def(rid="new")],
        BootstrapMode.CHECK,
    )
    assert [r.key for r in result.unchanged] == [("antigravity", "existing")]
    assert [r.key for r in result.added] == [("antigravity", "new")]
    assert [r.key for r in result.db_only] == [("gemini_cli", "db-only")]


async def test_write_mode_without_sink_rejected_at_plan_stage():
    source = FakeReadOnlySource([])
    service = ResourceBootstrapService(source)
    with pytest.raises(ResourceBootstrapError, match="sink"):
        await service.run([antigravity_def(rid="r1")], BootstrapMode.IMPORT)
    with pytest.raises(ResourceBootstrapError, match="sink"):
        await service.run([antigravity_def(rid="r1")], BootstrapMode.OVERWRITE)
    # CHECK still works sink-less.
    result = await service.run([antigravity_def(rid="r1")], BootstrapMode.CHECK)
    assert [r.key for r in result.added] == [("antigravity", "r1")]


async def test_injected_memory_source_is_plan_equivalent_to_durable_source():
    """Same incoming definitions diffed against a memory seed vs. a
    durable (fake-PG) store produce identical plans — the service cannot
    tell the sources apart."""
    from core.resource_definition_repository import (
        MemoryResourceDefinitionRepository,
        ResourceRepositoryDefinitionSource,
    )

    seed = MemoryResourceDefinitionRepository([antigravity_def(rid="r1")])
    durable = PostgreSQLResourceRepository(
        FakeAsyncPostgres().connection_factory()
    )
    await durable.add(antigravity_def(rid="r1"))

    incoming = [antigravity_def(rid="r1"), antigravity_def(rid="new")]
    from_seed = await ResourceBootstrapService(seed).run(
        incoming, BootstrapMode.CHECK
    )
    from_durable = await ResourceBootstrapService(
        ResourceRepositoryDefinitionSource(durable)
    ).run(incoming, BootstrapMode.CHECK)
    assert [r.key for r in from_seed.added] == [r.key for r in from_durable.added]
    assert [r.key for r in from_seed.unchanged] == (
        [r.key for r in from_durable.unchanged]
    )
