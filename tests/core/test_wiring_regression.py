from __future__ import annotations

from pathlib import Path

from app.bootstrap import register_builtin_providers
from app.main import build_runtime
from core.provider_registry import ProviderRegistry
from providers.anonymous_vertex.provider import AnonymousVertexProvider
from providers.anonymous_vertex.resource import AnonymousVertexResource

from tests.conftest import make_chat_request


def _anon_config(models=None):
    cfg = {
        "scheduler": {"max_retries": 2, "cooldown": {}},
        "model_registry": {"refresh_interval": 300.0},
        "providers": {
            "anonymous_vertex": {
                "enabled": True,
                "resources": [{"id": "default"}],
            }
        },
    }
    if models is not None:
        cfg["providers"]["anonymous_vertex"]["models"] = models
    return cfg


async def test_closed_loop_fake_default_config():
    from config.loader import default_config

    scheduler = build_runtime(default_config())
    await scheduler.model_registry.refresh()
    assert "fake" in await scheduler.model_registry.providers_for("gemini-3.8-flash")
    assert "fake" in await scheduler.model_registry.providers_for("gemini-test")


async def test_closed_loop_anonymous_vertex_serves_gemini_38_flash():
    scheduler = build_runtime(_anon_config())
    index = await scheduler.model_registry.refresh()
    assert "anonymous_vertex" in index["gemini-3.8-flash"]
    assert "anonymous_vertex" in index["gemini-3.7-flash"]
    providers = await scheduler.model_registry.providers_for("gemini-3.8-flash")
    assert "anonymous_vertex" in providers


async def test_config_models_override_reaches_provider():
    scheduler = build_runtime(_anon_config(models=["custom-model-1", "custom-model-2"]))
    index = await scheduler.model_registry.refresh()
    assert index.get("custom-model-1") == ["anonymous_vertex"]
    assert index.get("custom-model-2") == ["anonymous_vertex"]
    assert "gemini-3.8-flash" not in index


async def test_scheduler_providers_for_resolves_model_to_pool():
    scheduler = build_runtime(_anon_config())
    await scheduler.model_registry.refresh()
    providers = await scheduler.model_registry.providers_for("gemini-3.8-flash")
    assert providers
    for pid in providers:
        assert pid in scheduler.pools


def test_factory_config_is_forwarded_by_registry():
    registry = ProviderRegistry()
    register_builtin_providers(registry)
    provider = registry.create(
        "anonymous_vertex",
        {"models": ["gemini-3.8-flash", "gemini-3.7-flash"]},
    )
    assert isinstance(provider, AnonymousVertexProvider)
    assert provider._models == ["gemini-3.8-flash", "gemini-3.7-flash"]


def test_registry_create_resources_builds_anon_resource():
    registry = ProviderRegistry()
    register_builtin_providers(registry)
    resources = registry.create_resources(
        "anonymous_vertex", [{"id": "default"}]
    )
    assert len(resources) == 1
    assert isinstance(resources[0], AnonymousVertexResource)
    assert resources[0].provider == "anonymous_vertex"


async def test_anonymous_vertex_full_chain_first_discovery():
    """TASK-002-FIX-02: gemini-3.8-flash completes the full chain

        Provider -> Model Discovery -> ModelRegistry -> Scheduler -> ResourcePool

    on a *fresh* registry (last_refresh is None). Under the old '0.0'
    initialisation the first Discovery could be skipped on hosts where
    time.monotonic() starts below the refresh interval; this test never calls
    model_registry.refresh() up-front and relies on the first-query Discovery
    triggered by Scheduler.chat_completion.
    """
    scheduler = build_runtime(_anon_config(models=["gemini-3.8-flash"]))
    assert scheduler.model_registry.last_refresh is None  # never refreshed yet

    provider = scheduler.providers["anonymous_vertex"]
    fixture = (
        Path(__file__).parent.parent
        / "fixtures"
        / "anonymous_vertex"
        / "response.json"
    ).read_bytes()

    class MockResponse:
        status_code = 200
        headers = {}

        def __init__(self, body: bytes) -> None:
            self.content = body

        async def aiter_bytes(self):
            yield self.content

    class MockClient:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, url, content=None, headers=None):
            self.calls += 1
            return MockResponse(fixture)

    client = MockClient()
    provider.set_http_client(client)

    async def _token_fetcher(resource):
        return "recaptcha-token"

    provider.set_token_fetcher(_token_fetcher)

    resp = await scheduler.chat_completion(
        make_chat_request(model="gemini-3.8-flash")
    )
    assert "Hello from Anonymous Vertex!" in resp.text
    assert client.calls == 1  # one upstream POST through the full chain
    assert scheduler.model_registry.last_refresh is not None  # discovery ran
    # The index now maps gemini-3.8-flash to the anonymous_vertex pool.
    assert scheduler.model_registry.snapshot()["gemini-3.8-flash"] == [
        "anonymous_vertex"
    ]
