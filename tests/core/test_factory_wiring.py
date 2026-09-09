"""TASK-001.5: Provider Registry, Resource Factory, and Application Wiring.

Verifies that Provider creation and Resource creation are fully decoupled
from the Application layer (main.py must never instantiate FakeProvider or
FakeResource directly as normal assembly logic).
"""

from __future__ import annotations

import pytest

from app.bootstrap import register_builtin_providers
from core.provider_registry import (
    ProviderDefinition,
    ProviderRegistry,
    UnknownProviderError,
)
from providers.fake import FakeProvider, FakeResource
from providers.fake.factory import FakeProviderFactory, FakeResourceFactory


# ---------------------------------------------------------------------------
# Test 1: Provider Registry -- 'fake' can be created through the registry.
# ---------------------------------------------------------------------------
def test_registry_can_create_fake_provider():
    registry = ProviderRegistry()
    register_builtin_providers(registry)
    provider = registry.create("fake", {})
    assert isinstance(provider, FakeProvider)


def test_registry_create_returns_fresh_instances():
    registry = ProviderRegistry()
    register_builtin_providers(registry)
    p1 = registry.create("fake", {})
    p2 = registry.create("fake", {})
    assert isinstance(p1, FakeProvider)
    assert isinstance(p2, FakeProvider)
    assert p1 is not p2


# ---------------------------------------------------------------------------
# Test 2: Unknown provider must raise a clear error, no fallback to fake.
# ---------------------------------------------------------------------------
def test_unknown_provider_raises():
    registry = ProviderRegistry()
    register_builtin_providers(registry)
    with pytest.raises(UnknownProviderError):
        registry.create("unknown", {})


def test_unknown_provider_resources_raises():
    registry = ProviderRegistry()
    register_builtin_providers(registry)
    with pytest.raises(UnknownProviderError):
        registry.create_resources("unknown", [{"id": "x"}])


def test_unknown_provider_never_falls_back_to_fake():
    registry = ProviderRegistry()
    register_builtin_providers(registry)
    with pytest.raises(UnknownProviderError):
        registry.create("definitely-not-a-provider", {})


# ---------------------------------------------------------------------------
# Test 3: Fake Resource Factory -- fake -> FakeResourceFactory -> FakeResource
# ---------------------------------------------------------------------------
def test_fake_resource_factory_builds_fake_resources():
    factory = FakeResourceFactory()
    resources = factory.create_resources(
        "fake",
        [
            {"id": "resource-01"},
            {"id": "resource-02", "scenario": "rate_limit"},
        ],
    )
    assert len(resources) == 2
    assert all(isinstance(r, FakeResource) for r in resources)
    assert [r.id for r in resources] == ["resource-01", "resource-02"]


def test_fake_resource_factory_sets_provider():
    factory = FakeResourceFactory()
    resources = factory.create_resources("fake", [{"id": "resource-01"}])
    assert resources[0].provider == "fake"


def test_registry_create_resources_delegates_to_resource_factory():
    registry = ProviderRegistry()
    register_builtin_providers(registry)
    resources = registry.create_resources(
        "fake", [{"id": "resource-01"}, {"id": "resource-02"}]
    )
    assert len(resources) == 2
    assert all(isinstance(r, FakeResource) for r in resources)


# ---------------------------------------------------------------------------
# Test 4: Application Wiring -- main.py must not instantiate FakeResource
# directly as assembly logic.  We prove the registry owns resource creation
# and that build_runtime wires providers/pools through the registry.
# ---------------------------------------------------------------------------
def test_main_does_not_import_concrete_types_for_assembly():
    import inspect

    import app.main

    src = inspect.getsource(app.main)
    # main.py may import from bootstrap, but must not contain direct
    # FakeProvider(...) / FakeResource(...) constructor calls or a
    # provider-specific factory map.
    assert "FakeProvider(" not in src
    assert "FakeResource(" not in src
    assert "_PROVIDER_FACTORIES" not in src
    assert "FakeProviderFactory" not in src
    assert "FakeResourceFactory" not in src


def test_build_runtime_wires_fake_through_registry():
    from config.loader import default_config

    from app.main import build_runtime

    scheduler = build_runtime(default_config())
    assert set(scheduler.providers.keys()) == {"fake"}
    pool = scheduler.pools["fake"]
    assert len(pool.resources) == 2
    assert all(isinstance(r, FakeResource) for r in pool.resources)


# ---------------------------------------------------------------------------
# Test 5: Provider / Resource Identity remains unique.
# ---------------------------------------------------------------------------
def test_identity_fake_resources_unique():
    from core.resource import ResourceKey

    registry = ProviderRegistry()
    register_builtin_providers(registry)
    resources = registry.create_resources(
        "fake", [{"id": "resource-01"}, {"id": "resource-02"}]
    )
    keys = {r.resource_key for r in resources}
    assert len(keys) == 2
    assert ResourceKey(provider="fake", id="resource-01") in keys
    assert ResourceKey(provider="fake", id="resource-02") in keys


def test_identity_distinct_providers_same_local_id():
    from core.resource import ResourceKey

    fake_reg = ProviderRegistry()
    register_builtin_providers(fake_reg)
    fake_res = fake_reg.create_resources("fake", [{"id": "shared"}])

    # Simulate a second provider (firebase) owning the same local id.
    other = FakeResource(id="shared", provider="firebase", scenario="success")
    assert fake_res[0].resource_key != other.resource_key
    assert fake_res[0].resource_key == ResourceKey(provider="fake", id="shared")
    assert other.resource_key == ResourceKey(provider="firebase", id="shared")


# ---------------------------------------------------------------------------
# ProviderDefinition wiring sanity checks.
# ---------------------------------------------------------------------------
def test_provider_definition_holds_both_factories():
    definition = ProviderDefinition(
        provider_id="fake",
        provider_factory=FakeProviderFactory(),
        resource_factory=FakeResourceFactory(),
    )
    assert definition.provider_id == "fake"
    provider = definition.provider_factory.create_provider("fake")
    assert isinstance(provider, FakeProvider)
    resources = definition.resource_factory.create_resources(
        "fake", [{"id": "resource-01"}]
    )
    assert isinstance(resources[0], FakeResource)
