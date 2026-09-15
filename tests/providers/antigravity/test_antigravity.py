import json
from unittest.mock import MagicMock

import httpx

from providers.antigravity.client import AntigravityClient
from providers.antigravity.factory import AntigravityFactory
from providers.antigravity.model_discovery import ModelDiscovery
from providers.antigravity.provider import AntigravityProvider
from providers.antigravity.resource import AntigravityResource


def test_resource_defaults():
    res = AntigravityResource(id="a")
    assert res.ide_type == "ANTIGRAVITY"
    assert res.provider == "antigravity"


def test_client_uses_bearer_and_v1internal():
    res = AntigravityResource(id="a", access_token="tok-1")

    def fake_post(url, **kwargs):
        assert url == "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"
        assert kwargs["headers"]["Authorization"] == "Bearer tok-1"
        return httpx.Response(200, json={"models": {"gemini-test": {"displayName": "Test", "model": "gemini-test"}}})

    client = AntigravityClient(res, client=MagicMock(post=fake_post))
    data = client.fetch_available_models()
    assert data["models"]["gemini-test"]["model"] == "gemini-test"


def test_model_discovery_parses_modelinfo():
    res = AntigravityResource(id="a", access_token="tok-1")

    def fake_post(url, **kwargs):
        return httpx.Response(200, json={"models": {"gemini-test": {"displayName": "Test", "model": "gemini-test"}}})

    client = AntigravityClient(res, client=MagicMock(post=fake_post))
    discovery = ModelDiscovery(client)
    models = discovery.fetch_models()
    assert len(models) == 1
    assert models[0].id == "gemini-test"
    assert models[0].name == "Test"


def test_provider_list_models_returns_geminitest():
    res = AntigravityResource(id="a", access_token="tok-1")

    def fake_post(url, **kwargs):
        return httpx.Response(200, json={"models": {"gemini-test": {"displayName": "Test", "model": "gemini-test"}}})

    client = AntigravityClient(res, client=MagicMock(post=fake_post))
    provider = AntigravityProvider(res, client=client)
    models = provider.list_models()
    assert [m.id for m in models] == ["gemini-test"]


def test_registry_can_create_antigravity_provider():
    factory = AntigravityFactory()
    resource = factory.create_resource()
    provider = factory.create_provider(resource)
    assert isinstance(provider, AntigravityProvider)
    assert isinstance(provider.resource, AntigravityResource)
