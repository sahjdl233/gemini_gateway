"""End-to-end route tests through the FastAPI app (no real network)."""

from __future__ import annotations

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


def test_root_health():
    client = TestClient(create_app(make_app_config()))
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["service"] == "gemini-gateway"
    assert resp.json()["phase"] == "TASK-001"
