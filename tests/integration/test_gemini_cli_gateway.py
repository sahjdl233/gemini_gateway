"""Full Gateway chain: non-stream & stream (TASK-008)."""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from providers.gemini_cli.factory import GeminiCliProviderFactory
from providers.gemini_cli.provider import GeminiCliProvider
from tests.providers._gemini_cli_fakes import (
    FakeHttp,
    FakeResponse,
    envelope,
    make_resource,
    make_sse,
)


def gemini_cli_config(resources=None, models=None):
    if resources is None:
        resources = [
            {
                "id": "cli-01",
                "provider": "gemini_cli",
                "refresh_token": "refresh-token-1",
                "client_id": "client-id-1",
                "client_secret": "client-secret-1",
                "project_id": "gen-lang-client-test",
            }
        ]
    if models is None:
        models = ["gemini-2.5-flash"]
    return {
        "scheduler": {
            "max_retries": 1,
            "cooldown": {"base_delay": 0.1, "factor": 2.0, "max_delay": 5.0, "jitter": 0.1},
        },
        "providers": {
            "gemini_cli": {
                "enabled": True,
                "models": models,
                "resources": resources,
            }
        },
    }


def _make_app(http: FakeHttp, config=None):
    if config is None:
        config = gemini_cli_config()

    def fake_create_provider(self_factory, provider_id, cfg=None):
        models = None
        if cfg and isinstance(cfg, dict):
            models = cfg.get("models")
        provider = GeminiCliProvider(models=models)
        provider.set_http_client(http)
        return provider

    original = GeminiCliProviderFactory.create_provider
    GeminiCliProviderFactory.create_provider = fake_create_provider
    try:
        app = create_app(config)
    finally:
        GeminiCliProviderFactory.create_provider = original
    return app


def test_non_stream_gateway():
    http = FakeHttp()
    http.responses.append(http.token_ok("token-1", 3600))  # initial token refresh
    http.responses.append(
        http.ok(
            {
                "response": {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [{"text": "Gateway OK"}]
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 10,
                        "candidatesTokenCount": 5,
                        "thoughtsTokenCount": 0,
                        "totalTokenCount": 15,
                    },
                },
                "traceId": "trace-gateway-1",
            }
        )
    )

    app = _make_app(http)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gemini-2.5-flash", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["choices"][0]["message"]["content"] == "Gateway OK"
    assert data["usage"]["prompt_tokens"] == 10
    assert data["usage"]["completion_tokens"] == 5


def test_stream_gateway():
    http = FakeHttp()
    http.responses.append(http.token_ok("token-1", 3600))
    http.stream_responses.append(
        FakeResponse(
            200,
            sse_chunks=[
                make_sse(
                    envelope({"candidates": [{"content": {"parts": [{"text": "Hello"}]}}]}, trace="t1"),
                    envelope({"candidates": [{"content": {"parts": [{"text": " world"}]}}]}, trace="t2"),
                    envelope({"candidates": [{"content": {"parts": [{"text": ""}]}, "finishReason": "STOP"}]}, trace="t3"),
                )
            ],
        )
    )

    app = _make_app(http)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gemini-2.5-flash", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
    assert resp.status_code == 200
    content = resp.text
    chunks = [line for line in content.split("\n") if line.startswith("data: ")]
    # Last chunk is [DONE]
    assert chunks[-1] == "data: [DONE]"
    texts = []
    for chunk in chunks[:-1]:
        payload = json.loads(chunk[6:])
        if payload.get("choices"):
            delta = payload["choices"][0].get("delta", {})
            if "content" in delta and delta["content"]:
                texts.append(delta["content"])
    assert "".join(texts) == "Hello world"

