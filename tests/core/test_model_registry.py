"""Model Registry tests: TTL refresh and single-flight (TASK-001 task 3)."""

from __future__ import annotations

import asyncio
import time

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


def _force_expired(registry: ModelRegistry) -> None:
    """Backdate the last refresh so the registry reads as stale.

    Uses a clearly-past monotonic timestamp (rather than the 0.0 sentinel)
    so forcing expiry stays robust even on hosts where time.monotonic() at
    startup is smaller than the refresh interval (TASK-002-FIX-02).
    """
    registry._last_refresh = time.monotonic() - registry.refresh_interval - 5.0


async def test_first_build_lazy():
    prov = CountingProvider()
    registry = ModelRegistry(providers={"fake": prov}, refresh_interval=300.0)
    assert prov.list_calls == 0  # nothing called yet
    providers = await registry.providers_for("gemini-3.8-flash")
    assert "fake" in providers
    assert prov.list_calls == 1


async def test_first_query_always_performs_discovery():
    """A fresh registry must Discovery on its very first query, even when
    the refresh interval dwarfs the monotonic clock at startup."""
    prov = CountingProvider()
    # Refresh interval far larger than any reasonable monotonic() value: under
    # the old '0.0' initialisation this was mis-read as "still fresh" and the
    # first discovery was skipped. The never-refreshed (None) sentinel fixes it.
    registry = ModelRegistry(
        providers={"fake": prov}, refresh_interval=1_000_000.0
    )
    assert registry.last_refresh is None
    assert registry._is_expired() is True  # never refreshed is always stale

    providers = await registry.providers_for("gemini-3.8-flash")
    assert "fake" in providers
    assert prov.list_calls == 1  # discovery really happened
    assert registry.last_refresh is not None

    # And once refreshed, it is no longer stale.
    assert registry._is_expired() is False


async def test_first_query_discovery_when_monotonic_is_small(monkeypatch):
    """Regression (TASK-002-FIX-02): on hosts where the process starts with
    time.monotonic() below the refresh interval, the first query must still
    perform Discovery instead of returning an empty ModelRegistry index."""
    import core.model_registry as model_registry_module

    monkeypatch.setattr(
        model_registry_module.time, "monotonic", lambda: 0.5
    )
    prov = CountingProvider()
    registry = ModelRegistry(providers={"fake": prov}, refresh_interval=300.0)
    assert registry._is_expired() is True  # never refreshed -> stale

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
    _force_expired(registry)
    await registry.providers_for("gemini-3.8-flash")
    assert prov.list_calls == 2


async def test_concurrent_refresh_single_flight():
    """Multiple concurrent first queries must trigger only one refresh
    (the never-refreshed registry collapses into a single discovery)."""
    prov = CountingProvider()
    registry = ModelRegistry(providers={"fake": prov}, refresh_interval=1.0)
    # Fresh registry: the differentiator for TASK-002-FIX-02 is that the
    # first access is always treated as stale, so no manual "expire" is needed.
    assert registry.last_refresh is None
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
