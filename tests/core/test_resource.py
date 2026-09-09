"""Resource abstraction tests (TASK-000 rules 4-5)."""

from __future__ import annotations

from core.health import HealthState
from core.resource import Resource
from providers.fake import FakeResource


def test_resource_defaults():
    res = Resource(id="r1", provider="fake")
    assert res.enabled is True
    assert res.health == HealthState.HEALTHY
    assert res.cooldown_until is None
    assert res.in_flight == 0
    assert res.total_requests == 0
    assert res.total_failures == 0
    assert res.consecutive_failures == 0


def test_fake_resource_extends_resource():
    res = FakeResource(id="r1", provider="fake", scenario="rate_limit", retry_after=2.0)
    assert isinstance(res, Resource)
    assert res.scenario == "rate_limit"
    assert res.retry_after == 2.0


def test_disabled_resource():
    res = Resource(id="r1", provider="fake", enabled=False)
    assert res.enabled is False
