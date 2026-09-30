"""ResourceDefinition DTO layer tests (DB-RESOURCE-001-1).

Design baseline: docs/DB-RESOURCE-DESIGN-001.md (rev. 2).

These tests cover the persistence boundary only.  They never touch
PostgreSQL, a Repository, startup bootstrap, or the Admin API.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.resource_definition import (
    AnonymousVertexResourceDefinition,
    AntigravityResourceDefinition,
    CredentialBearingProxyError,
    FakeResourceDefinition,
    FirebaseResourceDefinition,
    GeminiCliResourceDefinition,
    PROVIDER_DEFINITION_TYPES,
    ResourceDefinitionError,
    UnknownProviderError,
    parse_resource_definition,
    resource_definition_from_row,
)


# ---------------------------------------------------------------------------
# 1/2/3. Valid DTOs per provider, common fields, provider discriminant
# ---------------------------------------------------------------------------


def test_antigravity_valid_definition():
    dto = parse_resource_definition(
        {
            "provider": "antigravity",
            "id": "ag-1",
            "enabled": True,
            "credential_id": "cred-ag-1",
            "project_id": "proj-1",
            "ide_type": "ANTIGRAVITY",
        }
    )
    assert isinstance(dto, AntigravityResourceDefinition)
    assert dto.provider == "antigravity"
    assert dto.id == "ag-1"
    assert dto.enabled is True
    assert dto.credential_id == "cred-ag-1"
    assert dto.project_id == "proj-1"


def test_gemini_cli_valid_definition():
    dto = parse_resource_definition(
        {
            "provider": "gemini_cli",
            "id": "gcli-1",
            "enabled": True,
            "credential_id": "cred-gcli-1",
            "project_id": "proj-2",
            "tier": "FREE",
            "pinned_model": "gemini-3.8-flash",
            "ide_type": "GCLI",
            "platform": "PLATFORM_UNSPECIFIED",
            "plugin_type": "GEMINI",
            "preview": True,
        }
    )
    assert isinstance(dto, GeminiCliResourceDefinition)
    assert dto.tier == "FREE"
    assert dto.pinned_model == "gemini-3.8-flash"


def test_firebase_valid_definition():
    dto = parse_resource_definition(
        {
            "provider": "firebase",
            "id": "fb-1",
            "enabled": True,
            "credential_id": "cred-fb-1",
            "project_id": "proj-3",
            "pinned_model": "gemini-3.8-flash",
        }
    )
    assert isinstance(dto, FirebaseResourceDefinition)
    assert dto.project_id == "proj-3"


def test_anonymous_vertex_valid_definition():
    dto = parse_resource_definition(
        {
            "provider": "anonymous_vertex",
            "id": "av-1",
            "enabled": True,
            "credential_id": None,
            "proxy_scheme": "socks5",
            "proxy_host": "127.0.0.1",
            "proxy_port": 1080,
            "pinned_model": "gemini-3.8-flash",
        }
    )
    assert isinstance(dto, AnonymousVertexResourceDefinition)
    assert dto.proxy_scheme == "socks5"
    assert dto.proxy_host == "127.0.0.1"
    assert dto.proxy_port == 1080


def test_fake_valid_definition():
    dto = parse_resource_definition(
        {
            "provider": "fake",
            "id": "fake-1",
            "enabled": True,
            "credential_id": None,
            "scenario": "rate_limit",
            "retry_after": 1.5,
            "reply_text": "hi",
            "model_ids": ["gemini-3.8-flash"],
        }
    )
    assert isinstance(dto, FakeResourceDefinition)
    assert dto.scenario == "rate_limit"
    assert dto.retry_after == 1.5
    assert dto.reply_text == "hi"
    assert dto.model_ids == ["gemini-3.8-flash"]


def test_fake_defaults_are_available_without_every_field():
    dto = parse_resource_definition({"provider": "fake", "id": "fake-min"})
    assert dto.enabled is True
    assert dto.credential_id is None
    assert dto.scenario == "success"
    assert dto.reply_text == "Hello from FakeProvider!"


def test_common_fields_supported_by_every_provider():
    assert set(PROVIDER_DEFINITION_TYPES) == {
        "antigravity",
        "gemini_cli",
        "firebase",
        "anonymous_vertex",
        "fake",
    }
    for provider in PROVIDER_DEFINITION_TYPES:
        dto = parse_resource_definition(
            {
                "provider": provider,
                "id": "shared-id",
                "enabled": False,
                "credential_id": "cred-shared",
            }
        )
        assert dto.provider == provider
        assert dto.id == "shared-id"
        assert dto.enabled is False
        assert dto.credential_id == "cred-shared"


def test_same_id_under_different_providers_is_allowed():
    """Identity is composite (provider, id); id alone is not unique."""

    a = parse_resource_definition({"provider": "firebase", "id": "project-01"})
    b = parse_resource_definition({"provider": "fake", "id": "project-01"})
    assert a.id == b.id == "project-01"
    assert a.provider != b.provider


def test_provider_literal_cannot_be_overridden_to_another_provider():
    with pytest.raises(ValidationError):
        AntigravityResourceDefinition(
            provider="firebase", id="x"  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# 4. Unknown provider
# ---------------------------------------------------------------------------


def test_unknown_provider_is_hard_error():
    with pytest.raises(UnknownProviderError):
        parse_resource_definition({"provider": "vertex", "id": "x"})


def test_unknown_provider_is_not_downgraded_to_dict():
    with pytest.raises(ResourceDefinitionError):
        parse_resource_definition({"provider": "totally_unknown", "id": "x"})


def test_missing_provider_is_rejected():
    with pytest.raises(ResourceDefinitionError):
        parse_resource_definition({"id": "x"})


def test_blank_provider_is_rejected():
    with pytest.raises(ResourceDefinitionError):
        parse_resource_definition({"provider": "   ", "id": "x"})


def test_non_mapping_payload_is_rejected():
    with pytest.raises(ResourceDefinitionError):
        parse_resource_definition(["not", "a", "mapping"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5. Unknown field (strict extra="forbid")
# ---------------------------------------------------------------------------


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        parse_resource_definition(
            {
                "provider": "firebase",
                "id": "fb-unknown",
                "enabled": True,
                "credential_id": "cred-x",
                "totally_unknown": "value",
            }
        )


# ---------------------------------------------------------------------------
# 6. Runtime-only fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "runtime_field",
    [
        "health",
        "cooldown_until",
        "in_flight",
        "total_requests",
        "total_failures",
        "consecutive_failures",
    ],
)
@pytest.mark.parametrize("provider", ["antigravity", "gemini_cli", "firebase"])
def test_runtime_only_field_is_rejected(provider: str, runtime_field: str):
    with pytest.raises(ValidationError):
        parse_resource_definition(
            {
                "provider": provider,
                "id": "rt-1",
                "enabled": True,
                "credential_id": "cred-x",
                runtime_field: 1,
            }
        )


def test_runtime_only_in_flight_example_from_spec_is_rejected():
    with pytest.raises(ValidationError):
        parse_resource_definition(
            {
                "provider": "firebase",
                "id": "x",
                "enabled": True,
                "credential_id": "cred-x",
                "in_flight": 1,
            }
        )


# ---------------------------------------------------------------------------
# 7. Per-provider secret rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret_field",
    ["access_token", "refresh_token", "client_id", "client_secret", "token_expiry"],
)
def test_antigravity_secret_fields_rejected(secret_field: str):
    with pytest.raises(ValidationError):
        parse_resource_definition(
            {
                "provider": "antigravity",
                "id": "ag-secret",
                "enabled": True,
                "credential_id": "cred-x",
                secret_field: "secret-value",
            }
        )


@pytest.mark.parametrize(
    "secret_field",
    ["access_token", "refresh_token", "client_id", "client_secret", "token_expiry"],
)
def test_gemini_cli_oauth_fields_rejected(secret_field: str):
    with pytest.raises(ValidationError):
        parse_resource_definition(
            {
                "provider": "gemini_cli",
                "id": "gcli-secret",
                "enabled": True,
                "credential_id": "cred-x",
                secret_field: "secret-value",
            }
        )


@pytest.mark.parametrize("secret_field", ["api_key", "app_id", "debug_token"])
def test_firebase_secret_fields_rejected(secret_field: str):
    with pytest.raises(ValidationError):
        parse_resource_definition(
            {
                "provider": "firebase",
                "id": "fb-secret",
                "enabled": True,
                "credential_id": "cred-x",
                secret_field: "secret-value",
            }
        )


def test_firebase_app_id_is_rejected_explicitly():
    """app_id is Credential material (AUTH-010), never a definition field."""

    with pytest.raises(ValidationError):
        parse_resource_definition(
            {
                "provider": "firebase",
                "id": "fb-app-id",
                "enabled": True,
                "credential_id": "cred-x",
                "app_id": "1:1234:web:abc",
            }
        )


@pytest.mark.parametrize("secret_field", ["proxy_username", "proxy_password"])
def test_anonymous_vertex_proxy_credentials_rejected(secret_field: str):
    with pytest.raises(ValidationError):
        parse_resource_definition(
            {
                "provider": "anonymous_vertex",
                "id": "av-secret",
                "enabled": True,
                "credential_id": None,
                secret_field: "secret-value",
            }
        )


def test_fake_has_no_secret_fields_and_accepts_its_allowlist():
    dto = parse_resource_definition(
        {
            "provider": "fake",
            "id": "fake-all",
            "enabled": True,
            "credential_id": None,
            "scenario": "timeout",
            "retry_after": 2.0,
            "reply_text": "x",
            "model_ids": ["a", "b"],
        }
    )
    assert dto.scenario == "timeout"
    assert dto.model_ids == ["a", "b"]


# ---------------------------------------------------------------------------
# 10/11/12. Proxy validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["gemini_cli", "firebase"])
@pytest.mark.parametrize(
    "proxy",
    [
        "http://user:password@example.com",
        "socks5://user:pass@127.0.0.1:1080",
        "http://token@example.com",
        "socks5://127.0.0.1:1080?token=abc",
        "socks5://127.0.0.1:1080?api_key=abc",
        "http://example.com?password=hunter2",
        "http://example.com?access_token=abc",
    ],
)
def test_credential_bearing_proxy_rejected(provider: str, proxy: str):
    with pytest.raises(CredentialBearingProxyError):
        parse_resource_definition(
            {
                "provider": provider,
                "id": "proxy-secret",
                "enabled": True,
                "credential_id": "cred-x",
                "proxy": proxy,
            }
        )


@pytest.mark.parametrize("provider", ["gemini_cli", "firebase"])
@pytest.mark.parametrize(
    "proxy",
    [
        "http://127.0.0.1:8080",
        "socks5://127.0.0.1:1080",
        "socks5://proxy.internal:1080",
    ],
)
def test_credential_free_proxy_accepted(provider: str, proxy: str):
    dto = parse_resource_definition(
        {
            "provider": provider,
            "id": "proxy-ok",
            "enabled": True,
            "credential_id": "cred-x",
            "proxy": proxy,
        }
    )
    assert dto.to_runtime_definition()["proxy"] == proxy


def test_empty_proxy_is_rejected():
    with pytest.raises(ResourceDefinitionError):
        parse_resource_definition(
            {"provider": "firebase", "id": "p", "enabled": True, "proxy": "   "}
        )


def test_anonymous_vertex_credential_bearing_proxy_config_is_rejected():
    """Anonymous Vertex v1 has no Credential owner for proxy credentials."""

    with pytest.raises(ValidationError):
        parse_resource_definition(
            {
                "provider": "anonymous_vertex",
                "id": "av-proxy",
                "enabled": True,
                "credential_id": None,
                "proxy_scheme": "socks5",
                "proxy_host": "127.0.0.1",
                "proxy_port": 1080,
                "proxy_username": "user",
                "proxy_password": "pass",
            }
        )


def test_anonymous_vertex_credential_free_proxy_endpoint_allowed():
    dto = parse_resource_definition(
        {
            "provider": "anonymous_vertex",
            "id": "av-proxy-ok",
            "enabled": True,
            "credential_id": None,
            "proxy_scheme": "socks5",
            "proxy_host": "127.0.0.1",
            "proxy_port": 1080,
        }
    )
    assert dto.to_definition_json() == {
        "proxy_scheme": "socks5",
        "proxy_host": "127.0.0.1",
        "proxy_port": 1080,
    }


# ---------------------------------------------------------------------------
# 10. Type errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", ["maybe", "", None])
def test_enabled_type_error_is_rejected(bad_value):
    with pytest.raises(ValidationError):
        parse_resource_definition(
            {"provider": "firebase", "id": "fb-t", "enabled": bad_value}
        )


def test_id_type_error_is_rejected():
    with pytest.raises(ValidationError):
        parse_resource_definition({"provider": "firebase", "id": 123})


def test_credential_id_type_error_is_rejected():
    with pytest.raises(ValidationError):
        parse_resource_definition(
            {"provider": "firebase", "id": "fb-c", "credential_id": 5}
        )


def test_enabled_stays_boolean_and_does_not_coerce_strings():
    dto = parse_resource_definition(
        {"provider": "fake", "id": "f", "enabled": True}
    )
    assert dto.enabled is True
    with pytest.raises(ValidationError):
        parse_resource_definition({"provider": "fake", "id": "f", "enabled": "yes"})


# ---------------------------------------------------------------------------
# 9/13. Serialization / conversion
# ---------------------------------------------------------------------------


def test_to_definition_json_excludes_common_columns():
    dto = parse_resource_definition(
        {
            "provider": "antigravity",
            "id": "ag-1",
            "enabled": True,
            "credential_id": "cred-x",
            "project_id": "proj-1",
            "ide_type": "ANTIGRAVITY",
        }
    )
    assert dto.to_definition_json() == {
        "project_id": "proj-1",
        "ide_type": "ANTIGRAVITY",
    }


def test_to_runtime_definition_round_trips_through_provider_factory():
    """The emitted dict is exactly what ResourceFactory consumes."""

    from providers.antigravity.factory import AntigravityResourceFactory

    dto = parse_resource_definition(
        {
            "provider": "antigravity",
            "id": "ag-rt",
            "enabled": True,
            "credential_id": "cred-x",
            "project_id": "proj-1",
        }
    )
    payload = dto.to_runtime_definition()
    assert set(payload) == {
        "provider",
        "id",
        "enabled",
        "credential_id",
        "project_id",
        "ide_type",
    }
    resources = AntigravityResourceFactory().create_resources(
        "antigravity", [payload]
    )
    assert len(resources) == 1
    built = resources[0]
    assert built.id == "ag-rt"
    assert built.provider == "antigravity"
    assert built.credential_id == "cred-x"
    assert built.project_id == "proj-1"


def test_to_runtime_definition_carries_no_secret_or_runtime_state():
    dto = parse_resource_definition(
        {"provider": "firebase", "id": "fb-clean", "credential_id": "cred-x"}
    )
    payload = dto.to_runtime_definition()
    for forbidden in (
        "access_token",
        "refresh_token",
        "client_id",
        "client_secret",
        "token_expiry",
        "api_key",
        "app_id",
        "debug_token",
        "health",
        "cooldown_until",
        "in_flight",
        "total_requests",
        "total_failures",
        "consecutive_failures",
    ):
        assert forbidden not in payload


@pytest.mark.parametrize("provider", sorted(PROVIDER_DEFINITION_TYPES))
def test_round_trip_row_to_definition_json_to_row(provider: str):
    dto = parse_resource_definition(
        {"provider": provider, "id": "rt-1", "enabled": True, "credential_id": "c"}
    )
    restored = resource_definition_from_row(
        provider=provider,
        resource_id="rt-1",
        enabled=True,
        credential_id="c",
        definition=dto.to_definition_json(),
    )
    assert restored.to_definition_json() == dto.to_definition_json()
    assert restored.provider == dto.provider
    assert restored.id == dto.id


def test_definition_json_must_not_repeat_provider_column():
    with pytest.raises(ResourceDefinitionError):
        resource_definition_from_row(
            provider="firebase",
            resource_id="fb-1",
            enabled=True,
            credential_id=None,
            definition={"provider": "fake", "project_id": "p"},
        )


def test_credential_id_is_preserved_not_dropped_and_not_resolved():
    dto = parse_resource_definition(
        {"provider": "firebase", "id": "fb-c", "credential_id": "cred-missing"}
    )
    assert dto.credential_id == "cred-missing"
    assert dto.to_runtime_definition()["credential_id"] == "cred-missing"


def test_credential_id_empty_string_is_preserved_as_empty_string():
    """The DTO layer performs type validation only, per spec section 7.

    It does not invent a business rule that would rewrite an empty
    credential_id to None, nor does it resolve references.
    """

    dto = parse_resource_definition(
        {"provider": "firebase", "id": "fb-empty", "credential_id": ""}
    )
    assert dto.credential_id == ""


def test_dto_layer_does_not_import_credential_repository():
    import core.resource_definition as module

    source = open(module.__file__, encoding="utf-8").read()
    import_lines = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    ]
    # Docstrings may mention CredentialRepository as prose; no import may.
    assert not any("credential" in line.lower() for line in import_lines)
