"""CONFIG/R-4: persistence boundary contract (Definition / Credential / Runtime State).

The three layers and their frozen ownership:

* **ResourceDefinition** (core.resource_definition) — durable, provider,
  resource id, enabled, credential_id REFERENCE, provider-specific
  non-secret config.  Never secrets, never runtime state.
* **Credential material** (core.credential / CredentialRepository) —
  encrypted secrets, OAuth/refresh tokens, API keys.  The definition
  only carries the reference; the DTO layer never resolves it; runtime
  resources reach material only through the credential store
  (require_bound_credential, AUTH-013).
* **Resource runtime state** (core.resource.Resource + pool) — health,
  cooldown, failure counters, in-flight.  Discardable, rebuildable, and
  never part of resource identity.

These tests freeze that boundary at the DTO layer so it cannot regress.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.health import HealthState
from core.resource import Resource
from core.resource_definition import (
    AntigravityResourceDefinition,
    ResourceDefinitionError,
    parse_resource_definition,
)


# -- Test 1: definitions reject secret material ------------------------------------


@pytest.mark.parametrize(
    "secret_payload",
    [
        {"access_token": "xxx"},
        {"refresh_token": "xxx"},
        {"client_secret": "xxx"},
        {"api_key": "xxx"},
    ],
)
def test_definition_rejects_secret_fields(secret_payload):
    payload = {"provider": "antigravity", "id": "test", **secret_payload}
    # Field-level failures surface as the pydantic ValidationError
    # (re-raised by parse_resource_definition); payload/provider-level
    # problems raise ResourceDefinitionError.  Both are loud rejects.
    with pytest.raises((ResourceDefinitionError, ValidationError)):
        parse_resource_definition(payload)
    # And at the model layer directly (extra="forbid").
    with pytest.raises(ValidationError):
        AntigravityResourceDefinition(**payload)


# -- Test 2: definitions reject runtime fields -------------------------------------


@pytest.mark.parametrize(
    "runtime_payload",
    [
        {"cooldown_until": "2026-01-01T00:00:00Z"},
        {"total_failures": 5},
        {"total_requests": 5},
        {"consecutive_failures": 5},
        {"health": "DEGRADED"},
        {"in_flight": 1},
    ],
)
def test_definition_rejects_runtime_state_fields(runtime_payload):
    payload = {"provider": "antigravity", "id": "test", **runtime_payload}
    with pytest.raises((ResourceDefinitionError, ValidationError)):
        parse_resource_definition(payload)
    with pytest.raises(ValidationError):
        AntigravityResourceDefinition(**payload)


# -- Test 3: credential_id (the reference) is legal ---------------------------------


def test_definition_accepts_credential_reference():
    definition = parse_resource_definition(
        {
            "provider": "antigravity",
            "id": "test",
            "credential_id": "cred-001",
        }
    )
    assert definition.credential_id == "cred-001"
    # The reference is persisted as-is; the DTO layer never resolves it.
    # (to_definition_json omits None-valued body fields.)
    assert definition.to_definition_json() == {"ide_type": "ANTIGRAVITY"}


# -- Test 4: runtime state does not affect identity --------------------------------


def test_identity_is_provider_and_id_only():
    """Two definitions for the same (provider, id) share one identity no
    matter how their non-identity fields differ — and runtime state is
    not even representable on a definition."""
    first = AntigravityResourceDefinition(
        provider="antigravity", id="test", project_id="p-one"
    )
    second = AntigravityResourceDefinition(
        provider="antigravity",
        id="test",
        project_id="p-two",
        enabled=False,
        credential_id="cred-002",
    )
    assert (first.provider, first.id) == (second.provider, second.id)
    assert (first.provider, first.id) == ("antigravity", "test")

    # The runtime projection of the same identity is equally stable
    # across runtime state mutations: resource_key never moves.
    runtime = Resource(id="test", provider="antigravity")
    before = runtime.resource_key
    runtime.health = HealthState.COOLDOWN
    runtime.consecutive_failures = 9
    runtime.total_requests = 100
    runtime.total_failures = 40
    runtime.cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=60)
    assert runtime.resource_key == before


# -- Test 5: to_runtime_definition carries no runtime state -------------------------


def test_to_runtime_definition_is_runtime_state_free():
    definition = AntigravityResourceDefinition(
        provider="antigravity",
        id="test",
        project_id="p",
        credential_id="cred-001",
    )
    payload = definition.to_runtime_definition()

    assert payload["provider"] == "antigravity"
    assert payload["id"] == "test"
    assert payload["enabled"] is True
    assert payload["credential_id"] == "cred-001"
    assert payload["project_id"] == "p"

    for banned in (
        "health",
        "cooldown_until",
        "total_requests",
        "total_failures",
        "consecutive_failures",
        "in_flight",
        "last_error",
    ):
        assert banned not in payload


# -- Part C: the DTO module must never grow runtime/secret fields -------------------


def test_dto_module_declares_no_runtime_or_secret_fields():
    """Line-based guard over core/resource_definition.py ONLY (no
    repository-wide string scan): no field annotation for runtime state
    or secret material may appear in the definition module."""
    module = (
        Path(__file__).resolve().parents[2]
        / "core"
        / "resource_definition.py"
    )
    source = module.read_text(encoding="utf-8")
    banned_fields = (
        "health",
        "cooldown_until",
        "total_requests",
        "total_failures",
        "consecutive_failures",
        "in_flight",
        "last_error",
        "access_token",
        "refresh_token",
        "api_key",
    )
    pattern = re.compile(
        r"^\s*(" + "|".join(banned_fields) + r")\s*:", re.MULTILINE
    )
    hits = pattern.findall(source)
    assert not hits, (
        f"core/resource_definition.py declares runtime/secret field(s) "
        f"{hits} — the definition layer must stay reference-only and "
        "runtime-state-free (CONFIG/R-4)."
    )
