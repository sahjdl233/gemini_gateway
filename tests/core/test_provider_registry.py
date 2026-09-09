"""ProviderRegistry tests."""

from __future__ import annotations

import pytest

from core.provider_registry import ProviderRegistry
from providers.fake import FakeProvider


def test_register_has_list():
    reg = ProviderRegistry()
    reg.register("fake", FakeProvider)
    reg.register("firebase", lambda: FakeProvider())
    assert reg.has("fake")
    assert not reg.has("vertex")
    assert reg.list_ids() == ["fake", "firebase"]


def test_duplicate_register_raises():
    reg = ProviderRegistry()
    reg.register("fake", FakeProvider)
    with pytest.raises(ValueError):
        reg.register("fake", FakeProvider)


def test_unregister():
    reg = ProviderRegistry()
    reg.register("fake", FakeProvider)
    reg.unregister("fake")
    assert not reg.has("fake")


async def test_get_returns_instance():
    reg = ProviderRegistry()
    reg.register("fake", FakeProvider)
    provider = await reg.get("fake")
    assert isinstance(provider, FakeProvider)


async def test_get_caches_instance():
    reg = ProviderRegistry()
    reg.register("fake", FakeProvider)
    p1 = await reg.get("fake")
    p2 = await reg.get("fake")
    assert p1 is p2


async def test_get_unregistered_raises_keyerror():
    reg = ProviderRegistry()
    with pytest.raises(KeyError):
        await reg.get("missing")


async def test_list_instances_instantiates_all():
    reg = ProviderRegistry()
    reg.register("fake", FakeProvider)
    reg.register("fake2", FakeProvider)
    instances = await reg.list_instances()
    assert len(instances) == 2