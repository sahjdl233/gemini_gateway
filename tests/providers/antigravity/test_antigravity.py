from unittest.mock import AsyncMock

import httpx
import pytest

from providers.antigravity.client import AntigravityClient
from providers.antigravity.factory import AntigravityProviderFactory, AntigravityResourceFactory
from providers.antigravity.model_discovery import ModelDiscovery
from providers.antigravity.provider import AntigravityProvider
from providers.antigravity.resource import AntigravityResource

MODEL_RESPONSE = {"models": {"gemini-test": {"displayName": "Test", "model": "gemini-test"}}}


class RecordingBackend:
    """ExecutionBackend double: records requests, reuses one client object."""

    def __init__(self, response=None):
        self.calls = []
        self._response = response or httpx.Response(200, json=MODEL_RESPONSE)

    async def execute(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._response

    async def close(self):
        pass


def test_resource_defaults():
    res = AntigravityResource(id="a")
    assert res.ide_type == "ANTIGRAVITY"
    assert res.provider == "antigravity"


async def test_client_uses_bearer_and_v1internal():
    res = AntigravityResource(id="a", access_token="tok-1")
    backend = RecordingBackend()

    client = AntigravityClient(backend=backend)
    data = await client.fetch_available_models(res)
    assert data["models"]["gemini-test"]["model"] == "gemini-test"

    (call,) = backend.calls
    assert call["method"] == "POST"
    assert call["url"] == "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"
    assert call["headers"]["Authorization"] == "Bearer tok-1"
    assert call["json"] == {}


async def test_client_omits_authorization_without_token():
    backend = RecordingBackend()
    client = AntigravityClient(backend=backend)

    await client.fetch_available_models(AntigravityResource(id="a"))
    assert "Authorization" not in backend.calls[0]["headers"]


async def test_model_discovery_parses_modelinfo():
    res = AntigravityResource(id="a", access_token="tok-1")
    client = AntigravityClient(backend=RecordingBackend())
    discovery = ModelDiscovery(client)
    models = await discovery.fetch_models(res)
    assert len(models) == 1
    assert models[0].id == "gemini-test"
    assert models[0].provider == "antigravity"
    assert models[0].capabilities == {"stream": False, "tools": True}


def test_provider_does_not_bind_resource():
    client = AntigravityClient(backend=RecordingBackend())
    provider = AntigravityProvider(client=client)
    assert provider.client is client


def test_provider_creates_backend_per_provider_instance():
    p1 = AntigravityProvider()
    p2 = AntigravityProvider()
    assert p1.backend is not p2.backend
    assert p1.client._backend is p1.backend


def test_factory_ignores_resources_but_applies_transport_config():
    factory = AntigravityProviderFactory()
    provider = factory.create_provider(
        "antigravity",
        {"timeout_seconds": 42.5, "max_connections": 5, "resources": []},
    )
    assert isinstance(provider, AntigravityProvider)
    assert provider.client.timeout == 30.0

    # Config carries credentials too; none of it may reach the backend.
    credentially = {
        "resources": [{"id": "a1", "access_token": "leak"}],
        "access_token": "leak",
        "client_secret": "leak",
    }
    provider = factory.create_provider("antigravity", credentially)
    for field in ("access_token", "refresh_token", "client_secret", "cookie", "auth_user"):
        assert not hasattr(provider.backend, field)


def test_resource_factory_sets_defaults():
    resources = AntigravityResourceFactory().create_resources(
        "antigravity",
        [{"id": "a1", "access_token": "tok"}, {"id": "a2"}],
    )
    assert [r.id for r in resources] == ["a1", "a2"]
    assert all(r.provider == "antigravity" for r in resources)
    assert all(r.ide_type == "ANTIGRAVITY" for r in resources)


async def test_client_without_backend_fails_closed_no_per_request_client():
    """Regression: the old fallback created a fresh httpx.Client per call."""
    client = AntigravityClient(backend=None)
    with pytest.raises(RuntimeError, match="httpx.Client"):
        await client.fetch_available_models(AntigravityResource(id="a"))
