"""Config → ResourceDefinition adapter tests (DB-RESOURCE-003-2).

Exercises :func:`core.resource_definition_loader.load_resource_definitions`
over plain config mappings (the output shape of the existing config
loader) — no YAML files, no file I/O, no startup wiring.  A small
integration test feeds the loader's output straight into the 003-1
bootstrap service in CHECK mode.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from pydantic import ValidationError

from core.resource_bootstrap import BootstrapMode, ResourceBootstrapService
from core.resource_definition import (
    AnonymousVertexResourceDefinition,
    AntigravityResourceDefinition,
    FakeResourceDefinition,
    FirebaseResourceDefinition,
    GeminiCliResourceDefinition,
    UnknownProviderError,
)
from core.resource_definition_loader import (
    ResourceDefinitionLoadError,
    load_resource_definitions,
)
from tests.core.test_resource_postgres_crud import FakeAsyncPostgres


def config_with(provider: str, *resources: Dict[str, Any]) -> Dict[str, Any]:
    return {"providers": {provider: {"enabled": True, "resources": list(resources)}}}


# -- empty / missing shapes -------------------------------------------------------


def test_empty_config_returns_empty_list():
    assert load_resource_definitions({}) == []


def test_missing_providers_returns_empty_list():
    assert load_resource_definitions({"credential_store": {}}) == []


def test_providers_none_returns_empty_list():
    assert load_resource_definitions({"providers": None}) == []


def test_provider_without_resources_returns_empty_list():
    assert load_resource_definitions({"providers": {"fake": {"enabled": True}}}) == []


def test_empty_resources_list_returns_empty_list():
    assert load_resource_definitions(
        {"providers": {"fake": {"enabled": True, "resources": []}}}
    ) == []


# -- parsing and injection ----------------------------------------------------------


def test_single_provider_single_resource():
    defs = load_resource_definitions(
        config_with("antigravity", {"id": "r1", "project_id": "p1"})
    )
    assert len(defs) == 1
    assert isinstance(defs[0], AntigravityResourceDefinition)
    assert defs[0].provider == "antigravity"
    assert defs[0].id == "r1"
    assert defs[0].project_id == "p1"


def test_outer_provider_injected_when_entry_omits_it():
    defs = load_resource_definitions(
        config_with("gemini_cli", {"id": "g1", "tier": "paid"})
    )
    assert defs[0].provider == "gemini_cli"
    # The DTO carries the injected discriminator, not a per-entry copy.
    assert "provider" not in GeminiCliResourceDefinition(id="x").to_definition_json()


def test_matching_embedded_provider_accepted():
    defs = load_resource_definitions(
        config_with(
            "antigravity",
            {"id": "r1", "provider": "antigravity", "project_id": "p"},
        )
    )
    assert defs[0].provider == "antigravity"


def test_provider_mismatch_rejected_not_overwritten():
    with pytest.raises(ResourceDefinitionLoadError) as exc:
        load_resource_definitions(
            config_with(
                "gemini_cli",
                {"id": "r1", "provider": "firebase", "project_id": "p"},
            )
        )
    error = exc.value
    assert error.provider == "gemini_cli"
    assert error.resource_id == "r1"
    assert error.index == 0
    assert "firebase" in str(error) and "gemini_cli" in str(error)


def test_all_providers_parse():
    config = {
        "providers": {
            "antigravity": {"resources": [{"id": "a1", "project_id": "p"}]},
            "gemini_cli": {"resources": [{"id": "g1", "tier": "paid"}]},
            "firebase": {"resources": [{"id": "f1", "project_id": "fp"}]},
            "anonymous_vertex": {
                "resources": [
                    {"id": "v1", "proxy_scheme": "socks5",
                     "proxy_host": "10.0.0.1", "proxy_port": 1080}
                ]
            },
            "fake": {"resources": [{"id": "k1", "scenario": "failure"}]},
        }
    }
    defs = load_resource_definitions(config)
    assert {type(d) for d in defs} == {
        AntigravityResourceDefinition,
        GeminiCliResourceDefinition,
        FirebaseResourceDefinition,
        AnonymousVertexResourceDefinition,
        FakeResourceDefinition,
    }


# -- strict validation ----------------------------------------------------------


def test_unknown_provider_rejected_with_context():
    # An unknown provider section: parse_resource_definition must reject
    # the injected discriminator.
    with pytest.raises(ResourceDefinitionLoadError) as exc:
        load_resource_definitions(
            config_with("not_a_provider", {"id": "r1"})
        )
    assert isinstance(exc.value.__cause__, UnknownProviderError)
    assert exc.value.provider == "not_a_provider"
    assert exc.value.resource_id == "r1"
    assert exc.value.index == 0


def test_secret_field_rejected_and_preserved_as_cause():
    with pytest.raises(ResourceDefinitionLoadError) as exc:
        load_resource_definitions(
            config_with(
                "antigravity",
                {"id": "r1", "access_token": "tok", "project_id": "p"},
            )
        )
    assert isinstance(exc.value.__cause__, ValidationError)
    assert exc.value.resource_id == "r1"


def test_cross_provider_field_rejected():
    with pytest.raises(ResourceDefinitionLoadError):
        load_resource_definitions(
            config_with("gemini_cli", {"id": "g1", "proxy_scheme": "socks5"})
        )


def test_malformed_provider_specific_field_rejected():
    with pytest.raises(ResourceDefinitionLoadError):
        load_resource_definitions(
            # proxy with embedded credentials is rejected at the DTO layer
            config_with(
                "gemini_cli",
                {"id": "g1", "proxy": "http://user:pass@host:8080"},
            )
        )


def test_runtime_field_rejected():
    with pytest.raises(ResourceDefinitionLoadError):
        load_resource_definitions(
            config_with(
                "antigravity",
                {"id": "r1", "consecutive_failures": 3},
            )
        )


def test_legacy_credential_fields_not_stripped_to_make_entry_pass():
    """Legacy secret fields fail loudly — never silently popped."""
    for secret in ("refresh_token", "client_id", "client_secret"):
        with pytest.raises(ResourceDefinitionLoadError):
            load_resource_definitions(
                config_with("gemini_cli", {"id": "g1", secret: "x"})
            )


def test_malformed_entry_shape_rejected_with_index():
    with pytest.raises(ResourceDefinitionLoadError) as exc:
        load_resource_definitions(
            {"providers": {"fake": {"resources": [
                {"id": "ok1"},
                {"id": "ok2"},
                "not-a-mapping",
            ]}}}
        )
    assert exc.value.index == 2
    assert exc.value.provider == "fake"


# -- enabled handling ------------------------------------------------------------


def test_provider_enabled_false_does_not_drop_resources():
    config = {
        "providers": {
            "antigravity": {
                "enabled": False,
                "resources": [{"id": "r1", "project_id": "p"}],
            }
        }
    }
    defs = load_resource_definitions(config)
    assert len(defs) == 1
    assert defs[0].provider == "antigravity"
    # provider-level enabled is runtime config, never a DTO field
    assert not hasattr(defs[0], "provider_enabled")


def test_resource_enabled_preserved_verbatim():
    defs = load_resource_definitions(
        config_with(
            "antigravity",
            {"id": "on", "enabled": True},
            {"id": "off", "enabled": False},
        )
    )
    by_id = {d.id: d.enabled for d in defs}
    assert by_id == {"on": True, "off": False}


def test_resource_enabled_defaults_to_dto_default():
    defs = load_resource_definitions(config_with("antigravity", {"id": "r1"}))
    assert defs[0].enabled is True  # DTO's own default, not loader logic


def test_credential_id_preserved():
    defs = load_resource_definitions(
        config_with("antigravity", {"id": "r1", "credential_id": "cred-7"})
    )
    assert defs[0].credential_id == "cred-7"


# -- deterministic ordering ----------------------------------------------------------


def test_output_sorted_by_provider_then_id():
    config = {
        "providers": {
            "gemini_cli": {"resources": [{"id": "z"}, {"id": "a"}]},
            "antigravity": {"resources": [{"id": "b"}]},
        }
    }
    defs = load_resource_definitions(config)
    assert [(d.provider, d.id) for d in defs] == [
        ("antigravity", "b"),
        ("gemini_cli", "a"),
        ("gemini_cli", "z"),
    ]


# -- duplicate identity stays loader-neutral ------------------------------------------


def test_duplicate_identities_pass_through_to_bootstrap():
    """Two legal DTOs with the same identity are returned; 003-1 owns the
    duplicate-identity policy."""
    defs = load_resource_definitions(
        config_with(
            "antigravity",
            {"id": "r1", "project_id": "p1"},
            {"id": "r1", "project_id": "p2"},
        )
    )
    assert len(defs) == 2


# -- error context completeness -------------------------------------------------------


def test_error_context_carries_provider_index_resource_id():
    with pytest.raises(ResourceDefinitionLoadError) as exc:
        load_resource_definitions(
            {"providers": {"gemini_cli": {"resources": [
                {"id": "ok"},            # index 0
                {"id": "bad", "tier": 123, "proxy": ["not", "a", "string"]},
            ]}}}
        )
    error = exc.value
    assert (error.provider, error.resource_id, error.index) == (
        "gemini_cli", "bad", 1,
    )
    assert error.__cause__ is not None


# -- no file / YAML dependency --------------------------------------------------------


def test_loader_needs_no_file_io():
    """The loader works purely on mappings — its source uses no YAML and
    no pathlib file reading."""
    import core.resource_definition_loader as module

    source = open(module.__file__, encoding="utf-8").read()
    assert "import yaml" not in source
    assert "from pathlib" not in source
    assert "Path(" not in source


# -- 003-1 integration ---------------------------------------------------------------


async def test_loader_output_feeds_bootstrap_check():
    """config → loader → ResourceBootstrapService.run(CHECK) works with no
    glue code; the loader itself performs no writes."""
    config = {
        "providers": {
            "antigravity": {"resources": [{"id": "r1", "project_id": "p"}]},
            "gemini_cli": {"resources": [{"id": "g1", "tier": "paid"}]},
        }
    }
    defs = load_resource_definitions(config)
    from core.resource_postgres import PostgreSQLResourceRepository

    repo = PostgreSQLResourceRepository(FakeAsyncPostgres().connection_factory())
    service = ResourceBootstrapService.over_repository(repo)
    result = await service.run(defs, BootstrapMode.CHECK)
    assert [r.key for r in result.added] == [
        ("antigravity", "r1"),
        ("gemini_cli", "g1"),
    ]
    assert result.db_only == []
    assert not result.has_conflicts
