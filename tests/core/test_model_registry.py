"""Model Registry tests: TTL refresh and single-flight (TASK-001 task 3)."""

from __future__ import annotations

import asyncio
import time

import pytest

from core.errors import TimeoutError, UpstreamUnavailableError
from core.models import ModelInfo
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


# ---------------------------------------------------------------------------
# TASK-MODEL-001: Discovery failure must not empty a provider out of the index.
# ---------------------------------------------------------------------------


class ScriptedProvider(FakeProvider):
    """FakeProvider whose list_models() follows a scripted list of results.

    Each entry is either a list of model ids (Discovery success) or an
    exception instance to raise (Discovery failure). The last entry repeats
    once the script is exhausted, and the call count is tracked so tests can
    assert refresh behaviour.
    """

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        self.list_calls = 0

    async def list_models(self):
        self.list_calls += 1
        step = self.script[min(self.list_calls - 1, len(self.script) - 1)]
        if isinstance(step, BaseException):
            raise step
        return [ModelInfo(id=mid, provider="scripted") for mid in step]


async def _model_ids(registry: ModelRegistry) -> set:
    return {m.id for m in await registry.list_models()}


async def test_first_discovery_success_records_models():
    """First Discovery succeeds -> models are indexed, no failure recorded."""
    prov = ScriptedProvider([["m-a", "m-b"]])
    registry = ModelRegistry(providers={"p": prov}, refresh_interval=300.0)

    assert await _model_ids(registry) == {"m-a", "m-b"}
    assert registry.failures == {}
    assert registry.provider_status("p")["discovered"] is True
    assert registry.provider_status("p")["failed"] is False


async def test_first_discovery_failure_yields_no_models_but_is_recorded():
    """First-ever Discovery failure: no models, but distinguishable from an
    empty discovery via the failure channel."""
    prov = ScriptedProvider([UpstreamUnavailableError("boom", provider="p")])
    registry = ModelRegistry(providers={"p": prov}, refresh_interval=300.0)

    assert await _model_ids(registry) == set()
    assert "p" in registry.failures
    status = registry.provider_status("p")
    assert status["discovered"] is False
    assert status["failed"] is True
    assert status["last_error"] == "boom"


async def test_empty_discovery_is_distinct_from_failed_discovery():
    """A successful-but-empty Discovery must NOT be reported as a failure."""
    prov = ScriptedProvider([[]])
    registry = ModelRegistry(providers={"p": prov}, refresh_interval=300.0)

    assert await _model_ids(registry) == set()
    assert registry.failures == {}
    status = registry.provider_status("p")
    assert status["discovered"] is True
    assert status["failed"] is False


async def test_ttl_refresh_success_after_initial_failure():
    """First attempt fails, a later TTL refresh succeeds -> models appear."""
    prov = ScriptedProvider(
        [UpstreamUnavailableError("boom", provider="p"), ["m-a"]]
    )
    registry = ModelRegistry(providers={"p": prov}, refresh_interval=1.0)

    assert await _model_ids(registry) == set()
    assert "p" in registry.failures

    _force_expired(registry)
    assert await _model_ids(registry) == {"m-a"}
    assert registry.failures == {}
    assert registry.provider_status("p")["discovered"] is True


async def test_partial_failure_keeps_previous_models():
    """TTL refresh where one provider fails: the other provider's models stay,
    and the failing provider keeps its own last known good list."""
    good = ScriptedProvider([["g-1"]])
    flaky = ScriptedProvider(
        [["f-1"], UpstreamUnavailableError("net down", provider="b")]
    )
    registry = ModelRegistry(
        providers={"good": good, "flaky": flaky}, refresh_interval=300.0
    )

    assert await _model_ids(registry) == {"g-1", "f-1"}

    _force_expired(registry)
    assert await _model_ids(registry) == {"g-1", "f-1"}
    assert registry.failures == {"flaky": "net down"}
    assert registry.provider_status("flaky")["discovered"] is True
    assert registry.provider_status("flaky")["failed"] is True


async def test_all_providers_failing_keeps_entire_stale_index():
    """Every provider fails on refresh -> the whole stale index survives."""
    a = ScriptedProvider([["a-1"], UpstreamUnavailableError("x", provider="a")])
    b = ScriptedProvider([["b-1"], TimeoutError("y", provider="b")])
    registry = ModelRegistry(providers={"a": a, "b": b}, refresh_interval=300.0)

    assert await _model_ids(registry) == {"a-1", "b-1"}

    _force_expired(registry)
    assert await _model_ids(registry) == {"a-1", "b-1"}
    assert set(registry.failures) == {"a", "b"}


async def test_non_provider_error_does_not_abort_refresh():
    """A non-ProviderError (e.g. Antigravity's RuntimeError) is isolated to
    that provider and does not abort the whole refresh or lose good data."""
    good = ScriptedProvider([["g-1"]])
    boom = ScriptedProvider([RuntimeError("no backend")])
    registry = ModelRegistry(
        providers={"good": good, "boom": boom}, refresh_interval=300.0
    )

    assert await _model_ids(registry) == {"g-1"}
    assert registry.provider_status("boom")["failed"] is True


async def test_failure_then_recovery_via_list_models():
    """End-to-end: a failed refresh must not make a known model disappear."""
    prov = ScriptedProvider(
        [["gemini-3.8-flash"], UpstreamUnavailableError("blip", provider="p")]
    )
    registry = ModelRegistry(providers={"p": prov}, refresh_interval=1.0)

    assert "p" in await registry.providers_for("gemini-3.8-flash")
    _force_expired(registry)
    assert "p" in await registry.providers_for("gemini-3.8-flash")
    assert registry.failures == {"p": "blip"}
