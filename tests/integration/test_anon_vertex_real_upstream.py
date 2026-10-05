"""Real-upstream smoke test for anonymous_vertex (ANON-003).

Opt-in ONLY: skipped unless ``GEMINI_GATEWAY_REAL_ANON_VERTEX=1``.  CI and
the default test run never touch Google.

The test drives the production path — no bypass, no hand-built request::

    create_app()
      -> POST /v1/chat/completions (app route)
      -> Scheduler -> AnonymousVertexProvider
      -> _get_token() (real reCAPTCHA anchor+reload)
      -> AnonymousVertexClient -> HttpxTransport -> stock httpx -> real upstream

Recorded metrics are printed sanitized: no URL (carries the public key
query), no recaptcha token, no request body, response headers allowlisted.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GEMINI_GATEWAY_REAL_ANON_VERTEX") != "1",
    reason="real Google upstream; opt-in via GEMINI_GATEWAY_REAL_ANON_VERTEX=1",
)

CONFIG = {
    "scheduler": {"max_retries": 2},
    "providers": {
        "anonymous_vertex": {
            "enabled": True,
            "resources": [{"id": "default"}],
        }
    },
}

MODEL = "gemini-2.5-flash"
ENDPOINT_HOST = "cloudconsole-pa.clients6.google.com"

# Header allowlist for sanitized reporting (ANON-003: no cookies/tokens).
_SAFE_RESPONSE_HEADERS = {
    "content-type",
    "content-length",
    "date",
    "server",
    "alt-svc",
    "via",
    "cache-control",
}

_report: dict = {}


def _safe_headers(headers) -> dict:
    out = {}
    for key in headers:
        k = key.lower()
        if k in _SAFE_RESPONSE_HEADERS:
            out[k] = str(headers[k])[:80]
    return out


@pytest.fixture(scope="module")
def real_app():
    from app.main import create_app

    return create_app(CONFIG)


def test_01_tls_and_tcp_reachability():
    """Phase 0: TLS handshake facts for the upstream host (diagnostic)."""
    ctx = ssl.create_default_context()
    with socket.create_connection((ENDPOINT_HOST, 443), timeout=15) as sock:
        with ctx.wrap_socket(sock, server_hostname=ENDPOINT_HOST) as tls:
            _report["tls"] = {
                "version": tls.version(),
                "cipher": tls.cipher()[0],
            }
            print(f"\n[real-upstream] TLS ok: {tls.version()} {tls.cipher()[0]}")


def test_02_non_stream_hello(real_app):
    """Phase A + C: minimal non-streaming 'hello' through the full app path.

    Asserts whichever outcome the environment produces: a successful reply
    (transport accepted) OR the known TLS-fingerprint rejection
    ("Failed to verify action" -> AuthenticationError -> HTTP 401), which
    itself is the Phase-C real-failure evidence that the error chain works.
    """
    from fastapi.testclient import TestClient

    client = TestClient(real_app)
    t0 = time.perf_counter()
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "hello"}],
        },
        timeout=300.0,
    )
    total = time.perf_counter() - t0
    _report["non_stream"] = {
        "status": resp.status_code,
        "total_s": round(total, 2),
        "headers": _safe_headers(resp.headers),
    }
    print(
        f"\n[real-upstream] non-stream: status={resp.status_code} "
        f"total={total:.2f}s headers={_report['non_stream']['headers']}"
    )
    if resp.status_code == 200:
        body = resp.json()
        text = body["choices"][0]["message"]["content"]
        print(f"[real-upstream] non-stream reply: {text[:120]!r} "
              f"usage={body.get('usage')}")
        assert text
    else:
        # Real-failure classification check (Phase C): the upstream
        # rejection must surface as a classified OpenAI error body.
        err = resp.json().get("error", {})
        print(f"[real-upstream] non-stream failure (classified): {err}")
        assert resp.status_code == 401, err
        assert "Failed to verify action" in err.get("message", ""), err


def test_03_true_stream_first_frame(real_app):
    """Phase B: true incremental streaming (ANON-002 path), first-frame
    latency vs total."""
    from fastapi.testclient import TestClient

    client = TestClient(real_app)
    t0 = time.perf_counter()
    first_frame_s = None
    text_parts = []
    sse_events = 0
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
        timeout=300.0,
    ) as resp:
        status = resp.status_code
        headers = _safe_headers(resp.headers)
        assert status == 200, f"SSE status {status}"
        error_payload = None
        for line in resp.iter_lines():
            if not line:
                continue
            sse_events += 1
            if first_frame_s is None and line.startswith("data:"):
                first_frame_s = time.perf_counter() - t0
            if line == "data: [DONE]":
                break
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                if "error" in payload:
                    error_payload = payload["error"]
                    break
                choices = payload.get("choices") or []
                delta = (choices[0].get("delta") or {}) if choices else {}
                if delta.get("content"):
                    text_parts.append(delta["content"])
    total = time.perf_counter() - t0
    _report["stream"] = {
        "status": status,
        "first_frame_s": round(first_frame_s, 2) if first_frame_s else None,
        "total_s": round(total, 2),
        "sse_events": sse_events,
        "headers": headers,
        "error": error_payload,
    }
    print(
        f"\n[real-upstream] stream: status={status} "
        f"first_frame={first_frame_s and round(first_frame_s, 2)}s "
        f"total={total:.2f}s sse_events={sse_events}"
    )
    if text_parts:
        print(f"[real-upstream] stream reply: {''.join(text_parts)[:120]!r}")
        assert text_parts, "no content deltas received"
    else:
        # Known TLS-fingerprint rejection arriving inside the SSE body
        # (Phase C real failure, streaming variant): must stay classified.
        print(f"[real-upstream] stream failure (classified): {error_payload}")
        assert error_payload, "neither content nor a classified error frame"
        assert "Failed to verify action" in error_payload.get("message", ""), (
            error_payload
        )


def test_04_final_report():
    """Prints the consolidated sanitized report; always passes when the
    earlier phases produced results."""
    print("\n[real-upstream] consolidated report:", json.dumps(_report, indent=2))
    assert "non_stream" in _report or "stream" in _report
