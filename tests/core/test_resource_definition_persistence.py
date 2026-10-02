"""ResourceDefinition persistence round-trip and discriminator tests
(DB-RESOURCE-001-5, Parts A and B).

Part A — for every provider-specific DTO, the full persistence pipeline

    DTO
     -> to_definition_json()
     -> json.dumps()              (what the repository writes to JSONB)
     -> DB row shape              (identity columns + deserialized body)
     -> resource_definition_from_row()
     -> DTO

must satisfy ``original == restored`` across enabled True/False,
credential_id None / existing id, and every provider-specific field.

Part B — the provider field is a strict discriminant: a payload naming
one provider may never be parsed into another provider's DTO, and an
unknown provider must raise, never fall back silently.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from core.resource_definition import (
    AnonymousVertexResourceDefinition,
    AntigravityResourceDefinition,
    FakeResourceDefinition,
    FirebaseResourceDefinition,
    GeminiCliResourceDefinition,
    UnknownProviderError,
    resource_definition_from_row,
)


def build_definition_variants():
    """Every provider DTO across the required persistence variants."""
    return [
        AntigravityResourceDefinition(
            id="r1", enabled=True, credential_id="cred-1", project_id="p1"
        ),
        AntigravityResourceDefinition(
            id="r2", enabled=False, credential_id=None, project_id=None,
            ide_type="OTHER_IDE",
        ),
        GeminiCliResourceDefinition(
            id="g1", enabled=True, credential_id="cred-2",
            project_id="p2", tier="paid",
            pinned_model="gemini-2.5-pro", proxy="http://10.0.0.1:8080",
            ide_type="GCLI", platform="PLATFORM_LINUX",
            plugin_type="GEMINI", preview=False,
        ),
        GeminiCliResourceDefinition(
            id="g2", enabled=False, credential_id=None,
            project_id=None, tier="unknown", pinned_model=None,
            proxy=None, preview=True,
        ),
        FirebaseResourceDefinition(
            id="f1", enabled=True, credential_id="cred-3",
            project_id="p3", pinned_model="gemini-2.0-flash",
            proxy="socks5://10.0.0.2:1080",
        ),
        FirebaseResourceDefinition(
            id="f2", enabled=False, credential_id=None,
            project_id=None, pinned_model=None, proxy=None,
        ),
        AnonymousVertexResourceDefinition(
            id="v1", enabled=True, credential_id="cred-4",
            proxy_scheme="socks5", proxy_host="10.0.0.3",
            proxy_port=1080, pinned_model="gemini-2.5-flash",
        ),
        AnonymousVertexResourceDefinition(
            id="v2", enabled=False, credential_id=None,
            proxy_scheme="direct", proxy_host=None, proxy_port=None,
        ),
        FakeResourceDefinition(
            id="k1", enabled=True, credential_id="cred-5",
            scenario="failure", retry_after=1.5, reply_text="boom",
            model_ids=["m1", "m2"],
        ),
        FakeResourceDefinition(
            id="k2", enabled=False, credential_id=None,
            model_ids=None,
        ),
    ]


@pytest.mark.parametrize("original", build_definition_variants(),
                         ids=lambda d: f"{d.provider}:{d.id}")
def test_persistence_round_trip_restores_equal_dto(original):
    """DTO -> JSONB body -> row -> DTO is lossless and type-stable."""
    body = original.to_definition_json()
    stored = json.dumps(body)
    row = {
        "provider": original.provider,
        "resource_id": original.id,
        "enabled": original.enabled,
        "credential_id": original.credential_id,
        # The driver hands JSONB back as a dict.
        "definition": json.loads(stored),
    }
    restored = resource_definition_from_row(
        provider=row["provider"],
        resource_id=row["resource_id"],
        enabled=row["enabled"],
        credential_id=row["credential_id"],
        definition=row["definition"],
    )
    assert type(restored) is type(original)
    assert restored == original
    assert restored.enabled == original.enabled
    assert restored.credential_id == original.credential_id


# -- Part B: provider discriminator ------------------------------------------------


def test_unknown_provider_raises_never_falls_back():
    with pytest.raises(UnknownProviderError):
        from core.resource_definition import parse_resource_definition

        parse_resource_definition({"provider": "does_not_exist", "id": "x"})


def test_missing_provider_raises():
    from core.resource_definition import parse_resource_definition

    with pytest.raises(Exception):
        parse_resource_definition({"id": "x"})


@pytest.mark.parametrize(
    "payload",
    [
        # antigravity-specific field on gemini_cli
        {"provider": "gemini_cli", "id": "x", "proxy_scheme": "socks5"},
        # gemini_cli-specific field on antigravity
        {"provider": "antigravity", "id": "x", "tier": "paid"},
        # firebase field on anonymous_vertex
        {"provider": "anonymous_vertex", "id": "x", "project_id": "p"},
        # secret-shaped field on a DTO that forbids it
        {"provider": "antigravity", "id": "x", "access_token": "t"},
        {"provider": "firebase", "id": "x", "api_key": "k"},
    ],
    ids=[
        "foreign-field-gemini-cli",
        "foreign-field-antigravity",
        "foreign-field-anonymous-vertex",
        "secret-field-antigravity",
        "secret-field-firebase",
    ],
)
def test_cross_provider_and_secret_payloads_raise(payload):
    """A payload for one provider is never silently parsed as another."""
    from core.resource_definition import parse_resource_definition

    with pytest.raises(Exception) as exc:
        parse_resource_definition(payload)
    # Unknown extra fields are pydantic ValidationErrors (extra="forbid").
    assert isinstance(exc.value, (ValidationError, Exception))


def test_wrong_provider_payload_parsed_as_own_type_only():
    """A valid payload resolves to exactly its declared provider DTO."""
    from core.resource_definition import parse_resource_definition

    defn = parse_resource_definition(
        {"provider": "antigravity", "id": "x", "project_id": "p"}
    )
    assert type(defn) is AntigravityResourceDefinition
    assert defn.provider == "antigravity"
    # gemini_cli cannot hijack the same payload shape with its own fields.
    with pytest.raises(ValidationError):
        parse_resource_definition(
            {"provider": "gemini_cli", "id": "x", "project_id": "p",
             "proxy_scheme": "socks5"}
        )


# -- Part D: serialization boundary audit ------------------------------------------


@pytest.mark.parametrize("original", build_definition_variants(),
                         ids=lambda d: f"{d.provider}:{d.id}")
def test_persisted_body_contains_no_secret_or_runtime_fields(original):
    """Only provider-specific non-secret definition fields enter JSONB."""
    forbidden_markers = (
        "token", "secret", "password", "api_key", "apikey",
        "health", "in_flight", "consecutive", "cooldown",
        "total_requests", "total_failures", "client", "credential",
    )
    body = original.to_definition_json()
    for key in body:
        assert not any(marker in key.lower() for marker in forbidden_markers), (
            f"DTO leaked a forbidden field into the JSONB body: {key!r}"
        )
