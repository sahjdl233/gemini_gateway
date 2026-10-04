"""CONFIG/R-5/R-6: persistence repository contract tests.

The behavioural contract lives ONCE in :class:`RepositoryContractSuite`
and runs against every persistence implementation:

* memory (always — tests/core/test_repository_contract.py);
* PostgreSQL (opt-in — tests/test_r6_persistence_postgres.py, real
  server via GEMINI_GATEWAY_TEST_DATABASE_URL).

Guaranteed identical semantics: save/get roundtrip, replace, idempotent
delete, credential isolation (no aliasing between ids or through
returned payloads), runtime state independence (per ResourceKey,
optional by contract), plus the Part E reconciliation-source wiring.
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


class RepositoryContractSuite:
    """The implementation-agnostic persistence contract.

    Construct it with three repositories and run_all().  Every method is
    assert-based so both pytest (memory) and the opt-in PostgreSQL E2E
    run the exact same checks.
    """

    def __init__(
        self,
        *,
        definitions: ResourceDefinitionRepository,
        credentials: CredentialRepository,
        states: RuntimeStateStore,
        label: str = "",
    ) -> None:
        self.definitions = definitions
        self.credentials = credentials
        self.states = states
        self.label = label or "persistence"

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _definition(
        resource_id: str, project_id: str = "p"
    ) -> AntigravityResourceDefinition:
        return AntigravityResourceDefinition(
            provider="antigravity", id=resource_id, project_id=project_id
        )

    @staticmethod
    def _key(resource_id: str) -> ResourceKey:
        return ResourceKey(provider="antigravity", id=resource_id)

    # -- definition contract ---------------------------------------------------

    async def check_definition_roundtrip_and_list_order(self):
        await self.definitions.save(self._definition("r2"))
        await self.definitions.save(self._definition("r1"))

        stored = await self.definitions.get("antigravity", "r1")
        assert stored == self._definition("r1")
        listed = await self.definitions.list_all()
        assert [d.id for d in listed] == ["r1", "r2"]  # deterministic order

    async def check_definition_save_replaces_and_delete_is_idempotent(self):
        await self.definitions.save(self._definition("r1", project_id="p-old"))
        await self.definitions.save(self._definition("r1", project_id="p-new"))
        stored = await self.definitions.get("antigravity", "r1")
        assert stored.project_id == "p-new"

        await self.definitions.delete("antigravity", "r1")
        assert await self.definitions.get("antigravity", "r1") is None
        await self.definitions.delete("antigravity", "r1")  # no-op, no raise

    async def check_definition_get_unknown_is_none(self):
        assert await self.definitions.get("antigravity", "missing") is None

    async def check_definition_keys_are_independent(self):
        """Same id under two providers (or two ids) never collides — the
        composite key is (provider, resource_id)."""
        await self.definitions.save(self._definition("r1"))
        await self.definitions.save(
            GeminiCliResourceDefinition(
                provider="gemini_cli", id="r1", tier="PRO"
            )
        )
        await self.definitions.save(self._definition("r2"))

        first = await self.definitions.get("antigravity", "r1")
        second = await self.definitions.get("gemini_cli", "r1")
        assert first.project_id == "p"
        assert second.tier == "PRO"
        assert len(await self.definitions.list_all()) == 3

    # -- credential contract -----------------------------------------------------

    async def check_credential_roundtrip(self):
        material = CredentialMaterial(
            type="oauth",
            payload={"refresh_token": "rt", "client_id": "ci"},
        )
        await self.credentials.save_secret("cred-1", material)
        fetched = await self.credentials.get_secret("cred-1")
        assert fetched.type == "oauth"
        assert fetched.payload == {"refresh_token": "rt", "client_id": "ci"}

    async def check_credential_get_unknown_is_none(self):
        assert await self.credentials.get_secret("missing") is None

    async def check_credential_save_replaces(self):
        await self.credentials.save_secret(
            "cred-1", CredentialMaterial(type="oauth", payload={"v": 1})
        )
        await self.credentials.save_secret(
            "cred-1", CredentialMaterial(type="oauth", payload={"v": 2})
        )
        assert (await self.credentials.get_secret("cred-1")).payload == {
            "v": 2
        }

    async def check_credential_isolation_between_ids(self):
        """Two credentials never alias each other's material, and a
        mutated fetched payload never leaks back into the store."""
        await self.credentials.save_secret(
            "cred-a", CredentialMaterial(type="oauth", payload={"token": "a"})
        )
        await self.credentials.save_secret(
            "cred-b", CredentialMaterial(type="oauth", payload={"token": "b"})
        )
        assert (await self.credentials.get_secret("cred-a")).payload == {
            "token": "a"
        }
        assert (await self.credentials.get_secret("cred-b")).payload == {
            "token": "b"
        }

        fetched = await self.credentials.get_secret("cred-a")
        fetched.payload["token"] = "mutated"
        assert (
            await self.credentials.get_secret("cred-a")
        ).payload == {"token": "a"}

    # -- runtime state contract ----------------------------------------------------

    async def check_runtime_state_roundtrip(self):
        state = RuntimeState(
            resource_key=self._key("r1"),
            health=HealthState.DEGRADED,
            cooldown_until=datetime.now(timezone.utc) + timedelta(seconds=30),
            consecutive_failures=3,
            total_requests=17,
            total_failures=5,
        )
        await self.states.save(state)

        stored = await self.states.get(self._key("r1"))
        assert stored == state
        assert stored.health is HealthState.DEGRADED
        assert stored.total_requests == 17

    async def check_runtime_state_get_unknown_is_none(self):
        assert await self.states.get(self._key("missing")) is None

    async def check_runtime_state_keys_are_independent(self):
        await self.states.save(
            RuntimeState(resource_key=self._key("r1"), total_requests=1)
        )
        await self.states.save(
            RuntimeState(resource_key=self._key("r2"), total_requests=2)
        )

        assert (await self.states.get(self._key("r1"))).total_requests == 1
        assert (await self.states.get(self._key("r2"))).total_requests == 2

        # Overwriting one key leaves the other untouched.
        await self.states.save(
            RuntimeState(resource_key=self._key("r1"), total_requests=9)
        )
        assert (await self.states.get(self._key("r1"))).total_requests == 9
        assert (await self.states.get(self._key("r2"))).total_requests == 2

    async def check_runtime_state_is_optional_by_contract(self):
        """An empty store means 'no state', never an error — startup must
        not depend on this store having content."""
        assert await self.states.get(self._key("anything")) is None

    # -- runner ----------------------------------------------------------------------

    async def run_all(self) -> None:
        await self.check_definition_roundtrip_and_list_order()
        await self.check_definition_save_replaces_and_delete_is_idempotent()
        await self.check_definition_get_unknown_is_none()
        await self.check_definition_keys_are_independent()
        await self.check_credential_roundtrip()
        await self.check_credential_get_unknown_is_none()
        await self.check_credential_save_replaces()
        await self.check_credential_isolation_between_ids()
        await self.check_runtime_state_roundtrip()
        await self.check_runtime_state_get_unknown_is_none()
        await self.check_runtime_state_keys_are_independent()
        await self.check_runtime_state_is_optional_by_contract()


# -- memory implementation (always on) ------------------------------------------------


@pytest.fixture
def memory_suite() -> RepositoryContractSuite:
    return RepositoryContractSuite(
        definitions=MemoryResourceDefinitionRepository(),
        credentials=MemoryCredentialRepository(),
        states=MemoryRuntimeStateStore(),
        label="memory",
    )


@pytest.mark.asyncio
async def test_memory_persistence_contract(memory_suite):
    await memory_suite.run_all()


# -- protocol conformance --------------------------------------------------------------


def test_memory_implementations_satisfy_the_protocols(memory_suite):
    assert isinstance(memory_suite.definitions, ResourceDefinitionRepository)
    assert isinstance(memory_suite.credentials, CredentialRepository)
    assert isinstance(memory_suite.states, RuntimeStateStore)


# -- Part E: RuntimeReconciliationService reads via list_all ---------------------------


async def test_reconciliation_reads_the_new_repository_source():
    """The service accepts a CONFIG/R-5 repository directly: definitions
    flow through ``list_all()`` into the runtime builder — same
    reconciliation logic, new source."""
    definitions = MemoryResourceDefinitionRepository()
    await definitions.save(
        AntigravityResourceDefinition(
            provider="antigravity", id="r1", project_id="p"
        )
    )
    builder_calls: list = []

    def builder(definition_list):
        builder_calls.append(list(definition_list))
        return {
            "antigravity": [
                Resource(id=d.id, provider=d.provider)
                for d in definition_list
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
