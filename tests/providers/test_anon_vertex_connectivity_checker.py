"""ANON-009-A acceptance tests: connectivity admission checker."""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest

from providers.anonymous_vertex.admission import NodeAdmissionState
from providers.anonymous_vertex.admission_policy import (
    AdmissionPolicy,
    FailureCategory,
)
from providers.anonymous_vertex.checkers import (
    AnonymousVertexConnectivityChecker,
)
from providers.anonymous_vertex.node_definitions import NodeDefinition


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class FakeHttpClient:
    """Records every call; serves queued outcomes (status or exception)."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls: list = []

    async def _next(self, method, url, timeout=None):
        self.calls.append((method, url, timeout))
        outcome = (
            self._outcomes.pop(0)
            if len(self._outcomes) > 1
            else self._outcomes[0]
        )
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)

    async def head(self, url, timeout=None):
        return await self._next("HEAD", url, timeout)

    async def get(self, url, timeout=None):
        return await self._next("GET", url, timeout)


def _node(proxy=None):
    payload = {
        "scheme": "https",
        "host": "162.159.198.1",
        "port": 443,
    }
    if proxy is not None:
        payload = dict(proxy)
    return NodeDefinition(node_id="node-a", proxy=payload)


def _checker(client, **kwargs):
    return AnonymousVertexConnectivityChecker(client, **kwargs)


# ---------------------------------------------------------------------------
# Success / redirect
# ---------------------------------------------------------------------------
async def test_http_200_is_ready():
    client = FakeHttpClient([200])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.READY
    assert result.reason is None
    assert result.checked_at.tzinfo is not None
    assert client.calls == [
        ("HEAD", "https://162.159.198.1:443/", 10.0),
    ]


async def test_http_302_redirect_is_ready():
    client = FakeHttpClient([302])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.READY
    assert result.reason is None


# ---------------------------------------------------------------------------
# HTTP failures -> reason tokens
# ---------------------------------------------------------------------------
async def test_http_403_fails_with_status_reason():
    client = FakeHttpClient([403])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.FAILED
    assert result.reason == "http_status_403"


async def test_http_500_fails_with_status_reason():
    client = FakeHttpClient([500])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.FAILED
    assert result.reason == "http_status_500"


async def test_head_unsupported_falls_back_to_get():
    client = FakeHttpClient([405, 200])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.READY
    assert [m for m, _, _ in client.calls] == ["HEAD", "GET"]

    client = FakeHttpClient([501, 403])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.FAILED
    assert result.reason == "http_status_403"
    assert [m for m, _, _ in client.calls] == ["HEAD", "GET"]


# ---------------------------------------------------------------------------
# Transport-level failures
# ---------------------------------------------------------------------------
async def test_timeout_error_maps_to_timeout_reason():
    client = FakeHttpClient([asyncio.TimeoutError()])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.FAILED
    assert result.reason == "timeout"


async def test_httpx_timeout_maps_to_timeout_reason():
    client = FakeHttpClient([httpx.ReadTimeout("timed out")])
    result = await _checker(client).check(_node())
    assert result.reason == "timeout"


async def test_os_error_maps_to_network_error_reason():
    client = FakeHttpClient([OSError("no route to host")])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.FAILED
    assert result.reason == "network_error"


async def test_httpx_connect_error_maps_to_network_error_reason():
    client = FakeHttpClient([httpx.ConnectError("connection reset")])
    result = await _checker(client).check(_node())
    assert result.reason == "network_error"


# ---------------------------------------------------------------------------
# Cancellation propagates (ANON-008-B contract)
# ---------------------------------------------------------------------------
async def test_cancellation_propagates():
    class ParkedClient:
        async def head(self, url, timeout=None):
            await asyncio.sleep(30.0)

    checker = _checker(ParkedClient())
    task = asyncio.create_task(checker.check(_node()))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Injection & endpoint handling
# ---------------------------------------------------------------------------
async def test_checker_uses_injected_client_only():
    client = FakeHttpClient([200])
    checker = _checker(client, timeout_seconds=3.5)
    await checker.check(_node())
    assert client.calls[0] == ("HEAD", "https://162.159.198.1:443/", 3.5)
    # no internal client was created: the injected instance served the call
    assert checker._http is client


async def test_unprobeable_endpoint_fails_without_client_call():
    client = FakeHttpClient([200])
    for proxy in ({}, {"scheme": "https"}, {"scheme": "https", "host": "h"},
                  {"scheme": "direct", "host": "h", "port": 1}):
        result = await _checker(client).check(_node(proxy))
        assert result.state == NodeAdmissionState.FAILED
        assert result.reason == "invalid_endpoint"
    assert client.calls == []  # client never touched


async def test_definition_is_never_mutated():
    client = FakeHttpClient([200])
    node = _node()
    before = node.to_dict()
    await _checker(client).check(node)
    assert node.to_dict() == before


# ---------------------------------------------------------------------------
# Full domain chain: checker -> classifier -> policy (integration shape)
# ---------------------------------------------------------------------------
async def test_chain_into_classifier_and_policy():
    from providers.anonymous_vertex.admission_policy import (
        DefaultFailureClassifier,
    )

    policy = AdmissionPolicy(classifier=DefaultFailureClassifier())

    ok = await _checker(FakeHttpClient([200])).check(_node())
    assert policy.decide(ok).state == NodeAdmissionState.READY

    # http_status_403 is NOT yet recognized by the ANON-008-C default
    # classifier (this task may not modify it): it classifies as UNKNOWN
    # and stays FAILED/re-checkable.  Mapping status codes to categories
    # is an admission-classifier evolution, not checker logic.
    blocked = await _checker(FakeHttpClient([403])).check(_node())
    assert blocked.reason == "http_status_403"
    assert policy.decide(blocked).state == NodeAdmissionState.FAILED
    assert (
        DefaultFailureClassifier().classify(blocked) == FailureCategory.UNKNOWN
    )

    transient = await _checker(FakeHttpClient([OSError()])).check(_node())
    assert policy.decide(transient).state == NodeAdmissionState.FAILED
    assert (
        DefaultFailureClassifier().classify(transient) == FailureCategory.TRANSIENT
    )
