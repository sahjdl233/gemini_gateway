"""GATEWAY-002: /v1 client API key authentication.

Boundary contract under test:

* ``GET /v1/models`` and ``POST /v1/chat/completions`` pass through the
  ``require_client_auth`` seam BEFORE any scheduler interaction — an
  unauthenticated request is rejected with an OpenAI-style 401 and never
  reaches ``list_models`` / ``chat_completion`` / ``stream_chat``;
* the submitted key is never echoed back;
* ``/admin`` keeps its own ADMIN_TOKEN boundary, independent of the
  client API key;
* the default config (no ``api_auth`` section) stays open for local dev.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

CLIENT_KEY = "sk-gateway-test-key-123"
ADMIN_TOKEN = "test-admin-secret"


def _auth_config(**overrides) -> dict:
    config = {
        "providers": {
            "fake": {
                "enabled": True,
                "resources": [
                    {"id": "fake-01", "provider": "fake", "scenario": "success"},
                ],
            }
        },
        "api_auth": {
            "enabled": True,
            "api_keys": [CLIENT_KEY],
        },
    }
    config.update(overrides)
    return config


def _client(**overrides) -> TestClient:
    return TestClient(create_app(_auth_config(**overrides)))


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# /v1/models
# ---------------------------------------------------------------------------
def test_models_without_key_is_401():
    client = _client()
    resp = client.get("/v1/models")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "missing_api_key"


def test_models_with_wrong_key_is_401():
    client = _client()
    resp = client.get("/v1/models", headers=_bearer("sk-wrong"))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"


def test_models_with_correct_key_is_200():
    client = _client()
    resp = client.get("/v1/models", headers=_bearer(CLIENT_KEY))
    assert resp.status_code == 200
    assert any(m["id"] == "gemini-3.8-flash" for m in resp.json()["data"])


def test_models_non_bearer_scheme_is_401():
    client = _client()
    resp = client.get("/v1/models", headers={"Authorization": f"Basic {CLIENT_KEY}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_authorization_scheme"


# ---------------------------------------------------------------------------
# /v1/chat/completions — rejected BEFORE the scheduler
# ---------------------------------------------------------------------------
def _chat_payload(model="gemini-3.8-flash") -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
    }


def test_chat_without_key_is_401_before_scheduler():
    client = _client()
    # an unknown model would be a 404 once the scheduler/registry runs —
    # getting a 401 instead proves the seam rejected the call first
    resp = client.post("/v1/chat/completions", json=_chat_payload("gemini-9.9-ghost"))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_api_key"


def test_chat_with_wrong_key_is_401_before_scheduler():
    client = _client()
    resp = client.post(
        "/v1/chat/completions",
        json=_chat_payload("gemini-9.9-ghost"),
        headers=_bearer("sk-wrong"),
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"


def test_chat_with_correct_key_reaches_scheduler():
    client = _client()
    resp = client.post(
        "/v1/chat/completions", json=_chat_payload(), headers=_bearer(CLIENT_KEY)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"]


def test_chat_stream_with_correct_key_keeps_sse_behaviour():
    client = _client()
    payload = _chat_payload()
    payload["stream"] = True
    with client.stream(
        "POST", "/v1/chat/completions", json=payload, headers=_bearer(CLIENT_KEY)
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        lines = [line for line in resp.iter_lines() if line]
    assert any("chat.completion.chunk" in line for line in lines)
    assert lines[-1] == "data: [DONE]"


def test_chat_stream_without_key_is_401():
    client = _client()
    payload = _chat_payload()
    payload["stream"] = True
    resp = client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# no key echo
# ---------------------------------------------------------------------------
def test_error_responses_never_echo_the_submitted_key():
    client = _client()
    submitted = "sk-super-secret-probe-value"
    for resp in (
        client.get("/v1/models", headers=_bearer(submitted)),
        client.post(
            "/v1/chat/completions", json=_chat_payload(), headers=_bearer(submitted)
        ),
        client.get("/v1/models"),
        client.get("/v1/models", headers={"Authorization": f"Token {submitted}"}),
    ):
        assert submitted not in resp.text


# ---------------------------------------------------------------------------
# /admin boundary independence
# ---------------------------------------------------------------------------
def test_admin_still_requires_admin_token_even_with_valid_client_key(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)
    client = _client()
    resp = client.get("/admin/resources", headers=_bearer(CLIENT_KEY))
    assert resp.status_code in (401, 403)  # client key is NOT an admin token


def test_admin_accepts_admin_token_independently(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)
    client = _client()
    resp = client.get("/admin/resources", headers=_bearer(ADMIN_TOKEN))
    assert resp.status_code == 200
    # and the admin token is NOT valid on /v1 with a different key list
    resp = client.post(
        "/v1/chat/completions", json=_chat_payload(), headers=_bearer(ADMIN_TOKEN)
    )
    assert resp.status_code == 401  # client keys and ADMIN_TOKEN are disjoint


# ---------------------------------------------------------------------------
# default stays open (local dev) + env-var keys + fail-closed config
# ---------------------------------------------------------------------------
def test_default_config_has_no_v1_gating():
    client = TestClient(
        create_app(
            {
                "providers": {
                    "fake": {
                        "enabled": True,
                        "resources": [
                            {"id": "fake-01", "provider": "fake",
                             "scenario": "success"}
                        ],
                    }
                }
            }
        )
    )
    assert client.get("/v1/models").status_code == 200
    assert (
        client.post("/v1/chat/completions", json=_chat_payload()).status_code
        == 200
    )


def test_env_var_keys_are_accepted(monkeypatch):
    monkeypatch.setenv("GEMINI_GATEWAY_API_KEYS", "sk-env-key-1, sk-env-key-2")
    client = _client()  # config keys + env keys both valid
    assert (
        client.get("/v1/models", headers=_bearer("sk-env-key-1")).status_code
        == 200
    )
    assert (
        client.get("/v1/models", headers=_bearer("sk-env-key-2")).status_code
        == 200
    )
    assert client.get("/v1/models", headers=_bearer(CLIENT_KEY)).status_code == 200


def test_enabled_without_keys_is_a_startup_error():
    with pytest.raises(ValueError, match="at least one API key"):
        create_app(
            {
                "providers": {
                    "fake": {
                        "enabled": True,
                        "resources": [
                            {"id": "fake-01", "provider": "fake",
                             "scenario": "success"}
                        ],
                    }
                },
                "api_auth": {"enabled": True},
            }
        )
