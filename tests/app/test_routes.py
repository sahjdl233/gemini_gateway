"""End-to-end route tests through the FastAPI app (no real network)."""

from __future__ import annotations

from pathlib import Path
from fastapi.testclient import TestClient

from app.main import create_app


def make_app_config():
    return {
        "scheduler": {
            "max_retries": 2,
            "cooldown": {
                "base_delay": 0.2,
                "factor": 2.0,
                "max_delay": 10.0,
                "jitter": 0.1,
            },
        },
        "providers": {
            "fake": {
                "enabled": True,
                "resources": [
                    {"id": "fake-01", "provider": "fake", "scenario": "success"},
                    {
                        "id": "fake-02",
                        "provider": "fake",
                        "scenario": "rate_limit",
                        "retry_after": 1.0,
                    },
                ],
            }
        },
    }


def test_models_endpoint():
    client = TestClient(create_app(make_app_config()))
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert len(body["data"]) >= 1
    assert body["data"][0]["id"] == "gemini-3.8-flash"
    assert body["data"][0]["owned_by"] == "fake"


def test_chat_completion_non_stream():
    client = TestClient(create_app(make_app_config()))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.8-flash",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"]
    assert body["choices"][0]["finish_reason"] == "stop"


def test_chat_completion_stream():
    client = TestClient(create_app(make_app_config()))
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "gemini-3.8-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        lines = [line for line in resp.iter_lines() if line]
    assert lines[0].startswith("data: {")
    assert lines[-1] == "data: [DONE]"


def test_invalid_request_returns_400():
    client = TestClient(create_app(make_app_config()))
    resp = client.post("/v1/chat/completions", json={"messages": []})
    assert resp.status_code == 400


def test_unknown_model_returns_404():
    client = TestClient(create_app(make_app_config()))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "no-such-model",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 404


def test_all_rate_limited_returns_429():
    config = make_app_config()
    config["providers"]["fake"]["resources"] = [
        {"id": "fake-01", "provider": "fake", "scenario": "rate_limit", "retry_after": 1.0}
    ]
    config["scheduler"]["max_retries"] = 0
    client = TestClient(create_app(config))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.8-flash",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# TASK-002: Anonymous Vertex end-to-end through OpenAPI layer with a mock
# upstream (no real Google network).  Exercises application wiring:
# main -> registry -> AnonymousVertexProvider/Resource -> Scheduler -> routes.
# ---------------------------------------------------------------------------
def _mock_vertex_client():
    """httpx-AsyncClient-compatible mock returning the recorded fixture."""
    from tests.providers.test_anonymous_vertex import MockClient, MockResponse

    fixtures = Path(__file__).resolve().parent.parent / "fixtures" / "anonymous_vertex"
    with open(fixtures / "stream.txt", encoding="utf-8") as f:
        raw = f.read().encode()
    stream = [raw[i:i+20] for i in range(0, len(raw), 20)]
    return MockClient(responses={"post": MockResponse(200, stream_bytes=stream)})


def _make_anon_config():
    return {
        "scheduler": {
            "max_retries": 2,
            "cooldown": {
                "base_delay": 0.2,
                "factor": 2.0,
                "max_delay": 10.0,
                "jitter": 0.1,
            },
        },
        "providers": {
            "anonymous_vertex": {
                "enabled": True,
                "resources": [{"id": "default"}],
            }
        },
    }


def test_anon_vertex_wiring_builds_pool(monkeypatch):
    """build_runtime wires anonymous_vertex through the registry (no FakeResource)."""
    import app.main
    from providers.anonymous_vertex.provider import AnonymousVertexProvider

    async def _anon_token(resource):
        return "recaptcha-token"

    def fake_create_provider(self, provider_id, config=None):
        return AnonymousVertexProvider(
            http_client=_mock_vertex_client(),
            token_fetcher=_anon_token,
        )

    from providers.anonymous_vertex.factory import AnonymousVertexProviderFactory
    monkeypatch.setattr(AnonymousVertexProviderFactory, "create_provider", fake_create_provider)

    scheduler = app.main.build_runtime(_make_anon_config())
    assert set(scheduler.providers.keys()) == {"anonymous_vertex"}
    pool = scheduler.pools["anonymous_vertex"]
    assert len(pool.resources) == 1
    from providers.anonymous_vertex.resource import AnonymousVertexResource
    assert isinstance(pool.resources[0], AnonymousVertexResource)


def test_anon_vertex_chat_non_stream(monkeypatch):
    """POST /v1/chat/completions -> Scheduler -> AnonymousVertex(mock) -> OpenAI JSON."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from providers.anonymous_vertex.provider import AnonymousVertexProvider

    async def _anon_token(resource):
        return "recaptcha-token"

    def fake_create_provider(self, provider_id, config=None):
        return AnonymousVertexProvider(
            http_client=_mock_vertex_client(),
            token_fetcher=_anon_token,
        )

    from providers.anonymous_vertex.factory import AnonymousVertexProviderFactory
    monkeypatch.setattr(AnonymousVertexProviderFactory, "create_provider", fake_create_provider)

    client = TestClient(create_app(_make_anon_config()))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert "Hello" in body["choices"][0]["message"]["content"]


def test_anon_vertex_chat_stream(monkeypatch):
    """POST /v1/chat/completions?stream=true -> mock upstream -> OpenAI SSE."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from providers.anonymous_vertex.provider import AnonymousVertexProvider

    async def _anon_token(resource):
        return "recaptcha-token"

    def fake_create_provider(self, provider_id, config=None):
        return AnonymousVertexProvider(
            http_client=_mock_vertex_client(),
            token_fetcher=_anon_token,
        )

    from providers.anonymous_vertex.factory import AnonymousVertexProviderFactory
    monkeypatch.setattr(AnonymousVertexProviderFactory, "create_provider", fake_create_provider)

    client = TestClient(create_app(_make_anon_config()))
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        lines = [line for line in resp.iter_lines() if line]
    assert any("Hello" in line for line in lines)
    assert lines[-1] == "data: [DONE]"


def test_anon_vertex_unknown_model_no_pool():
    """A model not offered by anonymous_vertex => 404 (no fallback)."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    # No monkeypatch: real factory builds an AnonymousVertexProvider that never
    # touches the network because list_models is config-based.
    client = TestClient(create_app(_make_anon_config()))
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-9.9-ghost",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 404


def test_root_health():
    client = TestClient(create_app(make_app_config()))
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["service"] == "gemini-gateway"
    assert resp.json()["phase"] == "TASK-001"



# ---------------------------------------------------------------------------
# TASK-009: gemini_cli provider through gateway (full wiring test)
# ---------------------------------------------------------------------------


def _gemini_cli_config():
    """Config using gemini_cli provider (HTTP client injected via _build_http)."""
    return {
        "scheduler": {
            "max_retries": 2,
            "cooldown": {
                "base_delay": 0.2,
                "factor": 2.0,
                "max_delay": 10.0,
                "jitter": 0.1,
            },
        },
        "providers": {
            "gemini_cli": {
                "enabled": True,
                "resources": [
                    {
                        "id": "cli-account-01",
                        "provider": "gemini_cli",
                        "refresh_token": "test-refresh-token",
                        "client_id": "test-client-id",
                        "client_secret": "test-client-secret",
                        "project_id": "gen-lang-client-123",
                    }
                ],
                "models": ["gemini-2.5-flash"],
            }
        },
    }


def test_gemini_cli_non_stream_gateway():
    """gemini_cli complete -> OpenAI-compatible ChatResponse через gateway."""
    from unittest.mock import patch
    from app.main import create_app
    from providers.gemini_cli.provider import GeminiCliProvider
    from tests.providers._gemini_cli_fakes import FakeHttp
    import json

    http = FakeHttp()
    http.responses.append(http.token_ok("token-1", 3600))
    http.responses.append(
        http.ok(
            {
                "response": {
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": "Hello from Gemini"}]},
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 5,
                        "candidatesTokenCount": 3,
                        "totalTokenCount": 8,
                    },
                },
                "traceId": "t1",
            }
        )
    )

    with patch.object(GeminiCliProvider, "_build_http", return_value=http):
        client = TestClient(create_app(_gemini_cli_config()))
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-2.5-flash",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "Hello from Gemini"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 5
    assert body["usage"]["completion_tokens"] == 3


def test_gemini_cli_stream_gateway():
    """gemini_cli stream -> OpenAI SSE chunks via gateway."""
    from unittest.mock import patch
    from app.main import create_app
    from providers.gemini_cli.provider import GeminiCliProvider
    from tests.providers._gemini_cli_fakes import FakeHttp, FakeResponse
    import json

    http = FakeHttp()
    http.responses.append(http.token_ok("token-1", 3600))
    http.stream_responses.append(
        FakeResponse(
            200,
            sse_chunks=[
                'data: {"response": {"candidates": [{"content": {"parts": [{"text": "Hi"}]}}]}, "traceId": "t1"}\n\n'.encode(),
                'data: {"response": {"candidates": [{"content": {"parts": [{"text": " there"}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3, "totalTokenCount": 8}}, "traceId": "t1"}\n\n'.encode(),
            ],
        )
    )

    with patch.object(GeminiCliProvider, "_build_http", return_value=http):
        client = TestClient(create_app(_gemini_cli_config()))
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "gemini-2.5-flash",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        ) as resp:
            assert resp.status_code == 200
            lines = [line for line in resp.iter_lines() if line]

    assert lines[0].startswith("data: {")
    assert lines[-1] == "data: [DONE]"
    texts = []
    for line in lines[:-1]:
        if line.startswith("data: "):
            payload = json.loads(line[6:])
            if payload.get("choices"):
                delta = payload["choices"][0].get("delta", {})
                if "content" in delta and delta["content"]:
                    texts.append(delta["content"])
    assert "".join(texts) == "Hi there"
