"""Model Registry tests: TTL refresh and single-flight (TASK-001 task 3)."""

from __future__ import annotations

import asyncio

import pytest

from core.model_registry import ModelRegistry
from providers.fake import FakeProvider


class CountingProvider(FakeProvider):
    """FakeProvider that counts list_models() calls."""

    def __init__(self) -> None:
        super().__init__()
        self.list_calls = 0

    async def list_models(self):
        self.list_calls += 1
        return await super().list_models()


async def test_first_build_lazy():
    prov = CountingProvider()
    registry = ModelRegistry(providers={"fake": prov}, refresh_interval=300.0)
    assert prov.list_calls == 0  # nothing called yet
    providers = await registry.providers_for("gemini-3.8-flash")
    assert "fake" in providers
    assert prov.list_calls == 1


async def test_ttl_expiry_triggers_refresh():
    prov = CountingProvider()
    registry = ModelRegistry(providers={"fake": prov}, refresh_interval=1.0)
    await registry.providers_for("gemini-3.8-flash")
    assert prov.list_calls == 1
    # within TTL: no refresh
    await registry.providers_for("gemini-3.8-flash")
    assert prov.list_calls == 1
    # force expiry by manipulating last_refresh
    registry._last_refresh = 0.0
    await registry.providers_for("gemini-3.8-flash")
    assert prov.list_calls == 2


async def test_concurrent_refresh_single_flight():
    """Multiple concurrent queries must trigger only one refresh."""
    prov = CountingProvider()
    registry = ModelRegistry(providers={"fake": prov}, refresh_interval=1.0)
    # reset to force a refresh on next access
    registry._last_refresh = 0.0
    results = await asyncio.gather(
        *[registry.providers_for("gemini-3.8-flash") for _ in range(10)]
    )
    for r in results:
        assert "fake" in r
    assert prov.list_calls == 1


async def test_manual_refresh():
    prov = CountingProvider()
    registry = ModelRegistry(providers={"fake": prov}, refresh_interval=300.0)
    await registry.providers_for("gemini-3.8-flash")
    assert prov.list_calls == 1
    await registry.refresh()
    assert prov.list_calls == 2


async def test_snapshot_no_refresh():
    prov = CountingProvider()
    registry = ModelRegistry(providers={"fake": prov}, refresh_interval=300.0)
    assert registry.snapshot() == {}
    assert prov.list_calls == 0


async def test_list_models_aggregates_all():
    prov = CountingProvider()
    registry = ModelRegistry(providers={"fake": prov}, refresh_interval=300.0)
    models = await registry.list_models()
    ids = {m.id for m in models}
    assert "gemini-3.8-flash" in ids
    assert "gemini-test" in ids
    assert "gemini-other" in ids
