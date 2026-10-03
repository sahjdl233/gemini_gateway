"""Runtime resource build from ResourceDefinition DTOs (DB-RESOURCE-007).

Covers the switched runtime source and the frozen fallback:

* Part A — the conversion layer: DTO → to_runtime_definition() →
  ProviderRegistry.create_resources() → Resource;
* Part B — build_runtime: bootstrap enabled → resources come from the
  sink definitions (YAML entries ignored); disabled → legacy YAML path;
* Part D — bootstrap enabled is strict mode: legacy credential fields in
  config fail startup (fail-closed), while the same config passes with
  bootstrap disabled.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.main import build_runtime, create_app
from core.credential import CredentialStore
from core.resource_bootstrap import BootstrapMode, ResourceBootstrapError
from core.resource_definition import (
    AntigravityResourceDefinition,
    GeminiCliResourceDefinition,
)
from core.resource_definition_loader import ResourceDefinitionLoadError
from core.resource_definition_repository import (
    MemoryResourceDefinitionRepository,
    ResourceRepositoryDefinitionSource,
)
from core.resource_repository_memory import MemoryResourceRepository
from core.runtime_resource_factory import (
    create_runtime_resources,
    runtime_payloads_for_provider,
)
from core.scheduler import Scheduler
from tests.core.test_resource_bootstrap_startup import (
    SpySink,
    compose,
)


def antigravity_definition(rid: str = "r1", project_id: str = "p-dto"):
    return AntigravityResourceDefinition(
        id=rid,
        enabled=True,
        credential_id=None,
        project_id=project_id,
    )


# -- Part A: conversion layer ---------------------------------------------------


def test_runtime_payload_carries_identity_and_provider_body():
    payload = antigravity_definition().to_runtime_definition()
    assert payload == {
        "provider": "antigravity",
        "id": "r1",
        "enabled": True,
        "credential_id": None,
        "project_id": "p-dto",
        "ide_type": "ANTIGRAVITY",  # DTO allowlist default travels along
    }


def test_runtime_payloads_filtered_by_provider():
    payloads = runtime_payloads_for_provider(
        [antigravity_definition(), GeminiCliResourceDefinition(id="g1")],
        provider_id="antigravity",
    )
    assert len(payloads) == 1
    assert payloads[0]["id"] == "r1"


def test_definition_converts_to_runtime_resource():
    registry_registry = _fresh_registry()
    resources = create_runtime_resources(
        registry_registry,
        [antigravity_definition(rid="r1", project_id="p-x")],
        provider_id="antigravity",
    )
    assert len(resources) == 1
    resource = resources[0]
    assert resource.provider == "antigravity"
    assert resource.id == "r1"
    assert resource.enabled is True
    # The DTO body survived the conversion into the runtime object.
    assert resource.project_id == "p-x"
    # No runtime state is pre-written onto the definition.
    definition = antigravity_definition(rid="r1", project_id="p-x")
    assert definition.to_runtime_definition()["project_id"] == "p-x"


def _fresh_registry():
    from app.bootstrap import register_builtin_providers
    from core.provider_registry import ProviderRegistry

    registry = ProviderRegistry()
    register_builtin_providers(registry)
    return registry


# -- Part B: build_runtime source switch ----------------------------------------


def base_config() -> Dict[str, Any]:
    return {
        "providers": {
            "antigravity": {
                "enabled": True,
                "resources": [
                    # YAML says project "p-yaml" — must be ignored when
                    # the runtime source is the sink definitions.
                    {"id": "r1", "project_id": "p-yaml"},
                ],
            }
        }
    }


def test_bootstrap_enabled_builds_resources_from_definitions():
    sink = MemoryResourceRepository()
    definitions = [
        antigravity_definition(rid="r1", project_id="p-from-sink"),
    ]
    scheduler = build_runtime(
        base_config(),
        CredentialStore(),
        resource_definitions=definitions,
    )
    pool = scheduler.pools["antigravity"]
    assert [r.id for r in pool.resources] == ["r1"]
    # Sink wins over YAML: the runtime field comes from the DTO.
    assert pool.resources[0].project_id == "p-from-sink"


def test_bootstrap_disabled_keeps_yaml_path():
    scheduler = build_runtime(base_config(), CredentialStore())
    pool = scheduler.pools["antigravity"]
    assert [r.id for r in pool.resources] == ["r1"]
    assert pool.resources[0].project_id == "p-yaml"


def test_enabled_build_ignores_extra_yaml_resources():
    """The YAML resource list is not consulted at all when the runtime
    source is the sink: only sink definitions become Resources."""
    config = base_config()
    config["providers"]["antigravity"]["resources"].append(
        {"id": "yaml-only", "project_id": "p-yaml"}
    )
    scheduler = build_runtime(
        config,
        CredentialStore(),
        resource_definitions=[antigravity_definition(rid="r1")],
    )
    assert [r.id for r in scheduler.pools["antigravity"].resources] == ["r1"]


async def test_end_to_end_startup_composition_feeds_build():
    """factory → bootstrap apply → build_runtime, with a PRE-SEEDED sink
    (simulating durable state from a previous deployment): IMPORT reports
    the YAML drift as a conflict, does NOT overwrite, and the runtime is
    built from the sink's definition — the durable world wins over YAML."""
    sink = MemoryResourceRepository()
    await sink.add(antigravity_definition(rid="r1", project_id="p-dto"))
    definition_repo, service = compose(
        {
            **base_config(),  # YAML says project_id "p-yaml"
            "resource_bootstrap": {"enabled": True, "mode": "import"},
        },
        sink,
    )
    incoming = await definition_repo.list_definitions()
    result = await service.run(incoming, BootstrapMode.IMPORT)
    # Drift detected, sink untouched (IMPORT never overwrites).
    assert [c.key for c in result.conflicts] == [("antigravity", "r1")]
    final_definitions = await ResourceRepositoryDefinitionSource(
        sink
    ).list_definitions()
    scheduler = build_runtime(
        base_config(),
        CredentialStore(),
        resource_definitions=final_definitions,
    )
    pool = scheduler.pools["antigravity"]
    assert [(r.provider, r.id) for r in pool.resources] == [
        ("antigravity", "r1")
    ]
    assert pool.resources[0].project_id == "p-dto"


def test_sink_definitions_split_across_providers():
    """One sink listing drives multiple providers, each consuming only
    its own definitions."""
    scheduler = build_runtime(
        {
            "providers": {
                "antigravity": {"enabled": True, "resources": []},
                "fake": {"enabled": True, "resources": []},
            }
        },
        CredentialStore(),
        resource_definitions=[
            antigravity_definition(rid="r1"),
            GeminiCliResourceDefinition(id="g1"),
        ],
    )
    assert [r.id for r in scheduler.pools["antigravity"].resources] == ["r1"]
    # gemini_cli definition belongs to a provider section that does not
    # exist here — it is simply not built (per-provider sections decide
    # which providers run).
    assert "gemini_cli" not in scheduler.pools


# -- Part D: strict mode / fail-closed -------------------------------------------


def test_legacy_secret_fails_closed_when_bootstrap_enabled():
    config = {
        "providers": {
            "gemini_cli": {
                "enabled": True,
                "resources": [
                    {
                        "id": "g1",
                        "refresh_token": "legacy-secret",
                        "client_id": "legacy-id",
                        "client_secret": "legacy-secret",
                    }
                ],
            }
        },
        "resource_bootstrap": {"enabled": True, "mode": "import"},
    }
    with pytest.raises(ResourceDefinitionLoadError):
        create_app(config=config)


def test_same_legacy_config_passes_when_bootstrap_disabled():
    """The identical legacy config keeps working without bootstrap — the
    strict mode is opt-in."""
    config = {
        "providers": {
            "gemini_cli": {
                "enabled": True,
                "resources": [
                    {
                        "id": "g1",
                        "refresh_token": "legacy-secret",
                        "client_id": "legacy-id",
                        "client_secret": "legacy-secret",
                    }
                ],
            }
        }
    }
    app = create_app(config=config)
    pool = app.state.scheduler.pools["gemini_cli"]
    assert [r.id for r in pool.resources] == ["g1"]


def test_enabled_app_builds_runtime_from_sink_definitions():
    """Full create_app integration: enabled bootstrap applies config
    definitions to the (freshly created, empty) sink and builds the
    runtime from the sink's DTOs.  In the in-memory deployment the sink
    is seeded from the same config, so runtime values match the config —
    durable divergence over restarts needs the PostgreSQL sink."""
    config = {
        **base_config(),
        "resource_bootstrap": {"enabled": True, "mode": "import"},
    }
    app = create_app(config=config)
    result = app.state.resource_bootstrap_result
    assert result is not None and result.mode is BootstrapMode.IMPORT
    assert len(result.added) == 1
    # Runtime came from the sink DTO path (strictly validated), and the
    # sink on app.state holds the definitions that fed it.
    pool = app.state.scheduler.pools["antigravity"]
    assert [r.id for r in pool.resources] == ["r1"]
    assert pool.resources[0].project_id == "p-yaml"
    sink_defs = [
        d for d in _sync_list(app.state.resource_sink)
    ]
    assert [(d.provider, d.id) for d in sink_defs] == [("antigravity", "r1")]


def _seed_sink(definitions):
    """Create a MemoryResourceRepository pre-populated with DTOs (the
    durable state a previous deployment would have left behind)."""
    import asyncio

    async def _seed():
        sink = MemoryResourceRepository()
        for definition in definitions:
            await sink.add(definition)
        return sink

    return asyncio.run(_seed())


def _sync_list(sink):
    import asyncio

    return asyncio.run(sink.list())


def test_disabled_app_keeps_yaml_runtime_and_no_bootstrap_state():
    app = create_app(config=base_config())
    assert app.state.resource_bootstrap_result is None
    pool = app.state.scheduler.pools["antigravity"]
    assert pool.resources[0].project_id == "p-yaml"
