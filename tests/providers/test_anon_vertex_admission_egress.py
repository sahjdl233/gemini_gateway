"""ANON MVP hardening: per-node admission egress client selection.

Mocks ``build_client`` — no real SOCKS5/HTTP egress is contacted.  Verifies
that ``AnonymousVertexProvider._admission_client_for_node`` maps each
node's proxy configuration to the correct outbound proxy (never a target
URL), caches one client per node_id, and closes them all on close().
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from providers.anonymous_vertex.factory import AnonymousVertexProviderFactory
from providers.anonymous_vertex.node_definitions import NodeDefinition
from providers.anonymous_vertex.provider import AnonymousVertexProvider


class FakeClient:
    def __init__(self, tag):
        self.tag = tag
        self.closed = False

    async def aclose(self):
        self.closed = True


class FakeTransportConfig:
    """Captures TransportConfig kwargs for assertions."""

    def __init__(self, proxy=None, timeout_seconds=30.0):
        self.proxy = proxy
        self.timeout_seconds = timeout_seconds


def _provider_with_mocks(monkeypatch):
    """Production-path provider with build_client mocked to return
    tagged fakes and record every TransportConfig."""
    built = []

    def fake_build_client(config):
        client = FakeClient(f"client-{len(built)}")
        built.append((client, config))
        return client

    monkeypatch.setattr(
        "transport.http.build_client", fake_build_client
    )
    provider = AnonymousVertexProviderFactory().create_provider(
        "anonymous_vertex",
        {
            "enabled": True,
            "node_pool": {
                "nodes": [
                    {"id": "direct-node"},
                    {"id": "http-node",
                     "proxy": {"scheme": "http", "host": "10.0.0.1",
                               "port": 8080}},
                    {"id": "https-node",
                     "proxy": {"scheme": "https", "host": "10.0.0.2",
                               "port": 8443}},
                    {"id": "socks5-node",
                     "proxy": {"scheme": "socks5", "host": "10.0.0.3",
                               "port": 1080}},
                ],
            },
        },
    )
    return provider, built


def _definition(provider, node_id):
    return next(
        d for d in provider.node_definitions if d.node_id == node_id
    )


def test_direct_node_gets_proxyless_client(monkeypatch):
    provider, built = _provider_with_mocks(monkeypatch)
    client = provider._admission_client_for_node(
        _definition(provider, "direct-node")
    )
    config = built[-1][1]
    assert client is built[-1][0]
    assert config.proxy is None  # direct = no outbound proxy, never a URL


def test_http_and_https_nodes_map_to_proxy_urls(monkeypatch):
    provider, built = _provider_with_mocks(monkeypatch)
    provider._admission_client_for_node(_definition(provider, "http-node"))
    provider._admission_client_for_node(_definition(provider, "https-node"))
    http_cfg = built[-2][1]
    https_cfg = built[-1][1]
    assert http_cfg.proxy.as_url() == "http://10.0.0.1:8080"
    assert https_cfg.proxy.as_url() == "https://10.0.0.2:8443"
    # proxy config is transport routing, not a probe target string


def test_socks5_node_maps_to_socks5_proxy_url(monkeypatch):
    provider, built = _provider_with_mocks(monkeypatch)
    provider._admission_client_for_node(_definition(provider, "socks5-node"))
    config = built[-1][1]
    assert config.proxy.as_url() == "socks5://10.0.0.3:1080"


def test_clients_are_cached_per_node_id(monkeypatch):
    provider, built = _provider_with_mocks(monkeypatch)
    direct = _definition(provider, "direct-node")
    http = _definition(provider, "http-node")

    c1 = provider._admission_client_for_node(direct)
    c2 = provider._admission_client_for_node(direct)  # same node: cached
    assert c1 is c2
    assert len(built) == 1

    c3 = provider._admission_client_for_node(http)  # other node: own client
    assert c3 is not c1
    assert len(built) == 2
    assert len(provider._admission_clients) == 2


def test_close_closes_all_admission_clients(monkeypatch):
    provider, built = _provider_with_mocks(monkeypatch)
    for node_id in ("direct-node", "http-node", "https-node", "socks5-node"):
        provider._admission_client_for_node(_definition(provider, node_id))
    assert len(built) == 4

    asyncio.run(provider.close())
    assert all(client.closed for client, _ in built)
    assert provider._admission_clients == {}
    # idempotent: closing again must not raise
    asyncio.run(provider.close())
