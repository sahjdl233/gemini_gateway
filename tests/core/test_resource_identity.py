"""Resource identity (ResourceKey) tests (TASK-001 task 2)."""

from __future__ import annotations

from core.resource import ResourceKey
from providers.fake import FakeResource


def test_resource_key_format():
    key = ResourceKey(provider="firebase", id="project-01")
    assert str(key) == "firebase:project-01"


def test_resource_key_distinguishes_same_id_diff_provider():
    a = ResourceKey(provider="firebase", id="project-01")
    b = ResourceKey(provider="vertex", id="project-01")
    assert a != b
    assert a == ResourceKey(provider="firebase", id="project-01")


def test_resource_key_equality_and_hash():
    a = ResourceKey(provider="firebase", id="project-01")
    b = ResourceKey(provider="firebase", id="project-01")
    assert a == b
    assert hash(a) == hash(b)
    assert len({a, b}) == 1


def test_resource_property_returns_key():
    res = FakeResource(id="project-01", provider="firebase", scenario="success")
    assert res.resource_key == ResourceKey(provider="firebase", id="project-01")
    assert str(res.resource_key) == "firebase:project-01"


def test_same_local_id_different_providers_are_distinct_resources():
    a = FakeResource(id="project-01", provider="firebase", scenario="success")
    b = FakeResource(id="project-01", provider="vertex", scenario="success")
    assert a.id == b.id
    assert a.resource_key != b.resource_key
