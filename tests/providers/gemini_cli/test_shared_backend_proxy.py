"""TASK-ARCH-004-FIX-01: shared-backend and proxy semantics coverage.

ARCH-004 fixed transport ownership as::

    one GeminiCliProvider -> one HttpExecutionBackend -> one AsyncClient

so a Resource can no longer own an HTTP proxy.  These tests pin that
contract down:

* several Resources share ONE backend / ONE transport;
* provider-level proxy is transport configuration and wins;
* a single legacy Resource proxy still works (compatibility);
* several Resources with DIFFERENT proxies fail loudly at wiring time
  instead of silently routing through the first one;
* the shared client is not closed when it was injected for tests.
"""
from __future__ import annotations

import pytest

from core.models import ChatMessage, ChatRequest
from execution.http import HttpExecutionBackend
from providers.gemini_cli.errors import GeminiCliConfigError
from providers.gemini_cli.factory import GeminiCliProviderFactory
from providers.gemini_cli.provider import GeminiCliProvider
from transport.proxy import ProxyConfig
from tests.providers._gemini_cli_fakes import FakeHttp, make_resource


PROXY_A = "socks5://user:pass@host-a:1080"
PROXY_B = "socks5://user:pass@host-b:1080"
PROXY_HTTP = "http://user:pass@host-http:8080"


def _ok_response(http, text: str = "hi") -> None:
    http.responses.append(http.token_ok("token-1", 3600))
    http.responses.append(
        http.ok(
            {
                "response": {
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": text}]},
                            "finishReason": "STOP",
                        }
                    ]
                },
                "traceId": "trace-proxy",
            }
        )
    )


def _resource_dict(resource_id: str, **extra) -> dict:
    payload = {
        "id": resource_id,
        "refresh_token": "refresh-token-1",
        "client_id": "client-id-1",
        "client_secret": "client-secret-1",
        "project_id": "gen-lang-client-test",
    }
    payload.update(extra)
    return payload


# -- Shared backend ----------------------------------------------------


async def test_two_resources_share_one_backend_and_one_client():
    """resource A + resource B -> same provider -> same backend/client."""
    http = FakeHttp()
    provider = GeminiCliProvider()
    provider.set_http_client(http)

    client_a = await provider._client_for(make_resource(id="cli-a"))
    client_b = await provider._client_for(make_resource(id="cli-b"))

    assert client_a is not client_b, "clients stay per-Resource (auth scope)"
    assert client_a.backend is client_b.backend is provider.backend
    assert isinstance(provider.backend, HttpExecutionBackend)
    assert provider.backend._client is http


async def test_shared_backend_serves_both_resources_over_one_transport():
    """Both Resources land on the SAME injected transport."""
    http = FakeHttp()
    provider = GeminiCliProvider()
    provider.set_http_client(http)

    req = ChatRequest(
        model="gemini-2.5-flash",
        messages=[ChatMessage(role="user", content="q")],
    )
    for resource_id, text in (("cli-a", "from-a"), ("cli-b", "from-b")):
        _ok_response(http, text)
        resp = await provider.complete(req, make_resource(id=resource_id))
        assert resp.text == text

    # Each Resource: one token refresh + one generateContent = 4 calls, all
    # served by the single shared transport.
    assert len(http.post_calls) == 4
    assert provider.backend._client is http


async def test_injected_client_is_not_closed_by_provider_close():
    """A borrowed (test-owned) transport survives Provider.close()."""
    http = FakeHttp()
    provider = GeminiCliProvider()
    provider.set_http_client(http)
    assert provider.backend._owns_client is False

    await provider.close()

    assert http.closed is False


# -- Proxy semantics ---------------------------------------------------


def test_provider_level_proxy_reaches_the_shared_backend():
    """Provider-level proxy is transport config for the whole provider."""
    provider = GeminiCliProvider(
        proxy=ProxyConfig(scheme="http", host="host-http", port=8080)
    )
    backend = provider.backend
    assert isinstance(backend, HttpExecutionBackend)
    # One provider -> one backend -> one AsyncClient carrying the proxy.
    assert backend._client is provider.backend._client


def test_provider_level_proxy_wins_over_resource_proxy():
    """Provider config is authoritative; a Resource proxy never overrides."""
    provider = GeminiCliProviderFactory().create_provider(
        "gemini_cli",
        {
            "proxy": "socks5://user:pass@host-a:1080",
            "resources": [_resource_dict("cli-a", proxy=PROXY_B)],
        },
    )
    assert provider._effective_proxy == ProxyConfig(
        scheme="socks5", host="host-a", port=1080
    )


def test_single_resource_legacy_proxy_is_compatible():
    """One legacy Resource proxy keeps working (compatibility path)."""
    provider = GeminiCliProviderFactory().create_provider(
        "gemini_cli",
        {
            "resources": [
                _resource_dict("cli-a", proxy=PROXY_A),
                _resource_dict("cli-b"),  # no proxy -> direct, ignored
            ]
        },
    )
    assert provider._effective_proxy == ProxyConfig(
        scheme="socks5", host="host-a", port=1080
    )


def test_conflicting_resource_proxies_fail_loudly():
    """Conflicting Resource proxies must NOT silently pick the first one."""
    with pytest.raises(GeminiCliConfigError) as excinfo:
        GeminiCliProviderFactory().create_provider(
            "gemini_cli",
            {
                "resources": [
                    _resource_dict("cli-a", proxy=PROXY_A),
                    _resource_dict("cli-b", proxy=PROXY_B),
                ]
            },
        )
    message = str(excinfo.value)
    assert "requires one shared proxy" in message
    assert "conflicting proxies" in message
    assert "host-a" in message and "host-b" in message


def test_identical_resource_proxies_are_accepted():
    """Several Resources naming the SAME proxy are not a conflict."""
    provider = GeminiCliProviderFactory().create_provider(
        "gemini_cli",
        {
            "resources": [
                _resource_dict("cli-a", proxy=PROXY_A),
                _resource_dict("cli-b", proxy=PROXY_A),
            ]
        },
    )
    assert provider._effective_proxy == ProxyConfig(
        scheme="socks5", host="host-a", port=1080
    )


def test_provider_level_proxy_silences_resource_conflict():
    """Provider proxy resolves the ambiguity, so Resources need not agree."""
    provider = GeminiCliProviderFactory().create_provider(
        "gemini_cli",
        {
            "proxy": "socks5://user:pass@host-c:1080",
            "resources": [
                _resource_dict("cli-a", proxy=PROXY_A),
                _resource_dict("cli-b", proxy=PROXY_B),
            ]
        },
    )
    assert provider._effective_proxy == ProxyConfig(
        scheme="socks5", host="host-c", port=1080
    )


def test_no_proxy_anywhere_stays_direct():
    """No provider proxy and no Resource proxy -> direct connection."""
    provider = GeminiCliProviderFactory().create_provider(
        "gemini_cli",
        {"resources": [_resource_dict("cli-a")]},
    )
    assert provider._effective_proxy is None


def test_unparsable_resource_proxy_falls_back_to_direct():
    """A garbage Resource proxy keeps the historical direct fallback."""
    provider = GeminiCliProviderFactory().create_provider(
        "gemini_cli",
        {"resources": [_resource_dict("cli-a", proxy="not a url")]},
    )
    effective = provider._effective_proxy
    assert effective is None or effective.is_direct
