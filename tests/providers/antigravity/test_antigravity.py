from unittest.mock import AsyncMock

import httpx
import pytest
from core.errors import (
    AuthenticationError,
    AuthorizationError,
    InvalidRequestError,
    ModelNotFoundError,
    NetworkError,
    ProtocolError,
    RateLimitError,
    UpstreamUnavailableError,
)

from providers.antigravity.client import AntigravityClient
from providers.antigravity.factory import AntigravityProviderFactory, AntigravityResourceFactory
from providers.antigravity.model_discovery import ModelDiscovery
from providers.antigravity.provider import AntigravityProvider
from providers.antigravity.resource import AntigravityResource
from core.models import ChatMessage, ChatRequest

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


class RecordingStreamResponse:
    def __init__(self, status_code=200, chunks=()):
        self.status_code = status_code
        self.headers = {}
        self.chunks = list(chunks)
        self.closed = False

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk

    async def aiter_text(self):
        for chunk in self.chunks:
            yield chunk.decode("utf-8")

    async def aclose(self):
        self.closed = True


class StreamingBackend(RecordingBackend):
    def __init__(self, response):
        super().__init__()
        self._stream_response = response

    async def execute_stream(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._stream_response


def test_resource_defaults():
    res = AntigravityResource(id="a")
    assert res.ide_type == "ANTIGRAVITY"
    assert res.provider == "antigravity"


def test_function_tool_is_mapped_to_cloud_code_declarations():
    provider = AntigravityProvider(backend=RecordingBackend())
    request = ChatRequest(
        model="gemini-test",
        messages=[ChatMessage(role="user", content="weather")],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                },
            }
        ],
    )
    resource = AntigravityResource(id="a", project_id="project-a")

    payload = provider._build_payload(request, resource)

    assert payload["request"]["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ]
        }
    ]


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


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (400, InvalidRequestError),
        (401, AuthenticationError),
        (403, AuthorizationError),
        (404, ModelNotFoundError),
        (429, RateLimitError),
        (500, UpstreamUnavailableError),
        (502, NetworkError),
        (503, UpstreamUnavailableError),
    ],
)
async def test_client_maps_http_errors_to_core_errors(status_code, error_type):
    response = httpx.Response(status_code, json={"error": "upstream"})
    client = AntigravityClient(backend=RecordingBackend(response=response))
    with pytest.raises(error_type) as exc_info:
        await client.generate_content(
            AntigravityResource(id="a", access_token="tok"),
            {"project": "p", "request": {}},
        )
    assert exc_info.value.provider == "antigravity"
    assert exc_info.value.resource_id == "a"


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, AuthenticationError),
        (403, AuthorizationError),
        (404, ModelNotFoundError),
        (429, RateLimitError),
        (500, UpstreamUnavailableError),
        (502, NetworkError),
        (503, UpstreamUnavailableError),
    ],
)
async def test_stream_error_does_not_require_text_attribute(status_code, error_type):
    response = RecordingStreamResponse(status_code, [b"upstream error"])
    client = AntigravityClient(backend=StreamingBackend(response))
    with pytest.raises(error_type):
        await client.stream_generate_content(
            AntigravityResource(id="a", access_token="tok"),
            {"project": "p", "request": {}},
        )
    assert response.closed is True


async def test_client_maps_malformed_json_to_protocol_error():
    response = httpx.Response(200, content=b"not-json")
    client = AntigravityClient(backend=RecordingBackend(response=response))
    with pytest.raises(ProtocolError):
        await client.generate_content(AntigravityResource(id="a"), {})


async def test_stream_maps_malformed_sse_to_protocol_error_and_closes_response():
    response = RecordingStreamResponse(chunks=[b"data: {broken\n\n"])
    provider = AntigravityProvider(backend=StreamingBackend(response))
    request = ChatRequest(
        model="gemini-test",
        messages=[ChatMessage(role="user", content="hello")],
    )
    with pytest.raises(ProtocolError):
        async for _ in provider.stream(
            request, AntigravityResource(id="a", project_id="p")
        ):
            pass
    assert response.closed is True


async def test_model_discovery_parses_modelinfo():
    res = AntigravityResource(id="a", access_token="tok-1")
    client = AntigravityClient(backend=RecordingBackend())
    discovery = ModelDiscovery(client)
    models = await discovery.fetch_models(res)
    assert len(models) == 1
    assert models[0].id == "gemini-test"
    assert models[0].provider == "antigravity"
    assert models[0].capabilities == {"stream": False, "tools": True}


async def test_cold_start_discovery_uses_enabled_resource_source():
    backend = RecordingBackend()
    provider = AntigravityProvider(backend=backend)
    disabled = AntigravityResource(
        id="disabled", access_token="tok-disabled", enabled=False
    )
    enabled = AntigravityResource(
        id="enabled", access_token="tok-enabled", project_id="project-enabled"
    )
    provider.set_discovery_resource_source(lambda: [disabled, enabled])

    models = await provider.list_models()

    assert [model.id for model in models] == ["gemini-test"]
    assert backend.calls[0]["headers"]["Authorization"] == "Bearer tok-enabled"
    assert backend.calls[0]["json"] == {"project": "project-enabled"}


async def test_cold_start_discovery_without_enabled_resource_returns_empty():
    backend = RecordingBackend()
    provider = AntigravityProvider(backend=backend)
    provider.set_discovery_resource_source(
        lambda: [
            AntigravityResource(id="disabled", access_token="tok", enabled=False)
        ]
    )

    assert await provider.list_models() == []
    assert backend.calls == []


async def test_discovery_resource_keeps_its_own_token_and_project():
    backend = RecordingBackend()
    provider = AntigravityProvider(backend=backend)
    first = AntigravityResource(
        id="a", access_token="tok-a", project_id="project-a"
    )
    second = AntigravityResource(
        id="b", access_token="tok-b", project_id="project-b"
    )
    provider.set_discovery_resource_source(lambda: [first])
    await provider.list_models()
    provider.set_discovery_resource_source(lambda: [second])
    await provider.list_models()

    assert backend.calls[0]["headers"]["Authorization"] == "Bearer tok-a"
    assert backend.calls[0]["json"] == {"project": "project-a"}
    assert backend.calls[1]["headers"]["Authorization"] == "Bearer tok-b"
    assert backend.calls[1]["json"] == {"project": "project-b"}


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
