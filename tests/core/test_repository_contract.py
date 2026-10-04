"""CONFIG/R-5 Part F: persistence repository contract tests.

The same behavioural suite runs against every memory implementation of
the new persistence layer — this is the reference semantics the future
Postgres adapter must match (no SQLAlchemy, no migrations, no DB).

Covers: save/get roundtrip, delete, credential isolation (no aliasing
between ids, no aliasing through returned payloads), runtime state
independence (per ResourceKey, optional by contract), protocol
conformance, and the Part E source swap: RuntimeReconciliationService
reads definitions via ``list_all()`` when the repository provides it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.health import HealthState
from core.repositories import (
    CredentialMaterial,
    CredentialRepository,
    ResourceDefinitionRepository,
    RuntimeState,
    RuntimeStateStore,
)
from core.repositories.memory import (
    MemoryCredentialRepository,
    MemoryResourceDefinitionRepository,
    MemoryRuntimeStateStore,
)
from core.resource import Resource, ResourceKey
from core.resource_definition import (
    AntigravityResourceDefinition,
    GeminiCliResourceDefinition,
)
from core.runtime_reconciliation import RuntimeReconciliationService


# -- shared fixtures ---------------------------------------------------------------


@pytest.fixture
def definitions() -> MemoryResourceDefinitionRepository:
    return MemoryResourceDefinitionRepository()


@pytest.fixture
def credentials() -> MemoryCredentialRepository:
    return MemoryCredentialRepository()


@pytest.fixture
def states() -> MemoryRuntimeStateStore:
    return MemoryRuntimeStateStore()


def _definition(resource_id: str, project_id: str = "p") -> (
    AntigravityResourceDefinition
):
    return AntigravityResourceDefinition(
        provider="antigravity", id=resource_id, project_id=project_id
    )


def _key(resource_id: str) -> ResourceKey:
    return ResourceKey(provider="antigravity", id=resource_id)


# -- protocol conformance -----------------------------------------------------------


def test_memory_implementations_satisfy_the_protocols(
    definitions, credentials, states
):
    assert isinstance(definitions, ResourceDefinitionRepository)
    assert isinstance(credentials, CredentialRepository)
    assert isinstance(states, RuntimeStateStore)


# -- ResourceDefinitionRepository ----------------------------------------------------


async def test_definition_roundtrip_and_list_order(definitions):
    await definitions.save(_definition("r2"))
    await definitions.save(_definition("r1"))

    assert await definitions.get("antigravity", "r1") == _definition("r1")
    listed = await definitions.list_all()
    assert [d.id for d in listed] == ["r1", "r2"]  # deterministic order


async def test_definition_save_replaces_and_delete_is_idempotent(definitions):
    await definitions.save(_definition("r1", project_id="p-old"))
    await definitions.save(_definition("r1", project_id="p-new"))
    stored = await definitions.get("antigravity", "r1")
    assert stored.project_id == "p-new"

    await definitions.delete("antigravity", "r1")
    assert await definitions.get("antigravity", "r1") is None
    await definitions.delete("antigravity", "r1")  # no-op, no raise


async def test_definition_get_unknown_is_none(definitions):
    assert await definitions.get("antigravity", "missing") is None


async def test_definition_keys_are_independent(definitions):
    """Same id under two providers (or two ids) never collides — the
    composite key is (provider, resource_id)."""
    await definitions.save(_definition("r1"))
    other = GeminiCliResourceDefinition(
        provider="gemini_cli", id="r1", tier="PRO"
    )
    await definitions.save(other)
    await definitions.save(_definition("r2"))

    assert (await definitions.get("antigravity", "r1")).project_id == "p"
    assert (await definitions.get("gemini_cli", "r1")).tier == "PRO"
    assert len(await definitions.list_all()) == 3


# -- CredentialRepository ------------------------------------------------------------


async def test_credential_roundtrip(credentials):
    material = CredentialMaterial(
        type="oauth",
        payload={"refresh_token": "rt", "client_id": "ci"},
    )
    await credentials.save_secret("cred-1", material)
    fetched = await credentials.get_secret("cred-1")
    assert fetched.type == "oauth"
    assert fetched.payload == {"refresh_token": "rt", "client_id": "ci"}


async def test_credential_get_unknown_is_none(credentials):
    assert await credentials.get_secret("missing") is None


async def test_credential_save_replaces(credentials):
    await credentials.save_secret(
        "cred-1", CredentialMaterial(type="oauth", payload={"v": 1})
    )
    await credentials.save_secret(
        "cred-1", CredentialMaterial(type="oauth", payload={"v": 2})
    )
    assert (await credentials.get_secret("cred-1")).payload == {"v": 2}


async def test_credential_isolation_between_ids(credentials):
    """Two credentials never alias each other's material."""
    await credentials.save_secret(
        "cred-a", CredentialMaterial(type="oauth", payload={"token": "a"})
    )
    await credentials.save_secret(
        "cred-b", CredentialMaterial(type="oauth", payload={"token": "b"})
    )
    assert (await credentials.get_secret("cred-a")).payload == {"token": "a"}
    assert (await credentials.get_secret("cred-b")).payload == {"token": "b"}

    # Mutating a fetched payload must not leak back into the store...
    fetched = await credentials.get_secret("cred-a")
    fetched.payload["token"] = "mutated"
    assert (
        await credentials.get_secret("cred-a")
    ).payload == {"token": "a"}


# -- RuntimeStateStore ---------------------------------------------------------------


async def test_runtime_state_roundtrip(states):
    state = RuntimeState(
        resource_key=_key("r1"),
        health=HealthState.DEGRADED,
        cooldown_until=datetime.now(timezone.utc) + timedelta(seconds=30),
        consecutive_failures=3,
        total_requests=17,
        total_failures=5,
    )
    await states.save(state)

    stored = await states.get(_key("r1"))
    assert stored == state
    assert stored.health is HealthState.DEGRADED
    assert stored.total_requests == 17


async def test_runtime_state_get_unknown_is_none(states):
    assert await states.get(_key("missing")) is None


async def test_runtime_state_keys_are_independent(states):
    await states.save(RuntimeState(resource_key=_key("r1"), total_requests=1))
    await states.save(RuntimeState(resource_key=_key("r2"), total_requests=2))

    assert (await states.get(_key("r1"))).total_requests == 1
    assert (await states.get(_key("r2"))).total_requests == 2

    # Overwriting one key leaves the other untouched.
    await states.save(RuntimeState(resource_key=_key("r1"), total_requests=9))
    assert (await states.get(_key("r1"))).total_requests == 9
    assert (await states.get(_key("r2"))).total_requests == 2


async def test_runtime_state_is_optional_by_contract(states):
    """An empty store means 'no state', never an error — startup must
    not depend on this store having content."""
    assert await states.get(_key("anything")) is None


# -- Part E: RuntimeReconciliationService reads via list_all -------------------------


async def test_reconciliation_reads_the_new_repository_source(definitions):
    """The service accepts a CONFIG/R-5 repository directly: definitions
    flow through ``list_all()`` into the runtime builder — same
    reconciliation logic, new source."""
    await definitions.save(_definition("r1"))
    builder_calls: list = []

    def builder(definition_list):
        builder_calls.append(list(definition_list))
        return {
            "antigravity": [
                Resource(id=d.id, provider=d.provider) for d in definition_list
            ]
        }

    service = RuntimeReconciliationService(definitions, builder)
    snapshot = await service.reconcile()

    assert snapshot.source_count == 1
    assert [r.id for r in snapshot.resources_by_provider["antigravity"]] == [
        "r1"
    ]
    # One full read per reconcile.
    assert len(builder_calls) == 1
