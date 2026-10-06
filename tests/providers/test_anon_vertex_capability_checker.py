"""ANON-009-B acceptance tests: capability admission checker."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from providers.anonymous_vertex.admission import NodeAdmissionState
from providers.anonymous_vertex.checkers import (
    AnonymousVertexCapabilityChecker,
)
from providers.anonymous_vertex.node_definitions import NodeDefinition

from providers.anonymous_vertex.checkers.capability import _PROBE_URL


class FakeResponse:
    def __init__(self, status_code=200, body=None, raw=None):
        self.status_code = status_code
        self._body = body
        self._raw = raw

    def json(self):
        if self._raw is not None:
            raise json.JSONDecodeError("not json", self._raw, 0)
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeHttpClient:
    """Records every POST; serves queued outcomes."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls: list = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        outcome = (
            self._outcomes.pop(0)
            if len(self._outcomes) > 1
            else self._outcomes[0]
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _node():
    return NodeDefinition(
        node_id="node-a",
        proxy={"scheme": "https", "host": "162.159.198.1", "port": 443},
    )


def _checker(client, **kwargs):
    return AnonymousVertexCapabilityChecker(client, **kwargs)


# ---------------------------------------------------------------------------
# Valid capability response
# ---------------------------------------------------------------------------
async def test_valid_capability_response_is_ready():
    client = FakeHttpClient([FakeResponse(200, body={"candidates": [{}]})])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.READY
    assert result.reason is None
    assert result.checked_at.tzinfo is not None
    (call,) = client.calls
    assert call["url"] == _PROBE_URL  # the REAL batchGraphql endpoint
    assert call["timeout"] == 15.0


async def test_nested_vertex_shape_is_still_json_schema_only():
    """Only the schema verdict matters: candidates must be a list."""
    client = FakeHttpClient([FakeResponse(200, body={"candidates": []})])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.READY


# ---------------------------------------------------------------------------
# JSON-but-wrong-shape / non-JSON
# ---------------------------------------------------------------------------
async def test_json_without_candidates_is_invalid_capability_response():
    client = FakeHttpClient([FakeResponse(200, body={"message": "hello"})])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.FAILED
    assert result.reason == "invalid_capability_response"


async def test_empty_object_is_invalid_capability_response():
    client = FakeHttpClient([FakeResponse(200, body={})])
    result = await _checker(client).check(_node())
    assert result.reason == "invalid_capability_response"


async def test_html_body_is_invalid_response():
    client = FakeHttpClient([FakeResponse(200, raw="<html>blocked</html>")])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.FAILED
    assert result.reason == "invalid_response"


# ---------------------------------------------------------------------------
# HTTP failures
# ---------------------------------------------------------------------------
async def test_404_is_capability_not_found():
    client = FakeHttpClient([FakeResponse(404, body={"error": "no route"})])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.FAILED
    assert result.reason == "capability_not_found"


async def test_403_keeps_status_token():
    client = FakeHttpClient([FakeResponse(403, body={"error": "denied"})])
    result = await _checker(client).check(_node())
    assert result.reason == "http_status_403"


async def test_500_keeps_status_token():
    client = FakeHttpClient([FakeResponse(500, raw="oops")])
    result = await _checker(client).check(_node())
    assert result.reason == "http_status_500"


# ---------------------------------------------------------------------------
# Transport failures
# ---------------------------------------------------------------------------
async def test_timeout_maps_to_timeout_reason():
    client = FakeHttpClient([asyncio.TimeoutError()])
    result = await _checker(client).check(_node())
    assert result.reason == "timeout"

    client = FakeHttpClient([httpx.ReadTimeout("timed out")])
    result = await _checker(client).check(_node())
    assert result.reason == "timeout"


async def test_network_error_maps_to_network_error_reason():
    client = FakeHttpClient([httpx.ConnectError("connection reset")])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.FAILED
    assert result.reason == "network_error"


async def test_cancellation_propagates():
    class ParkedClient:
        async def post(self, url, json=None, headers=None, timeout=None):
            await asyncio.sleep(30.0)

    checker = _checker(ParkedClient())
    task = asyncio.create_task(checker.check(_node()))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Request boundary: headers & payload
# ---------------------------------------------------------------------------
async def test_headers_have_no_auth_material():
    client = FakeHttpClient([FakeResponse(200, body={"candidates": [{}]})])
    await _checker(client).check(_node())
    (call,) = client.calls
    headers = {k.lower() for k in (call["headers"] or {})}
    assert "content-type" in headers
    for forbidden in ("authorization", "cookie", "x-goog-user-project"):
        assert forbidden not in headers, forbidden
        assert not any(
            forbidden in k for k in (call["headers"] or {})
        ), forbidden


async def test_probe_payload_is_minimal_ping_without_user_data():
    client = FakeHttpClient([FakeResponse(200, body={"candidates": [{}]})])
    await _checker(client).check(_node())
    payload = client.calls[0]["json"]
    assert payload == {
        "contents": [{"role": "user", "parts": [{"text": "ping"}]}]
    }
    assert payload["contents"][0]["role"] == "user"
    assert payload["contents"][0]["parts"][0]["text"] == "ping"
    # fixed probe text — never user content
    assert payload["contents"][0]["parts"][0]["text"] == "ping"


# ---------------------------------------------------------------------------
# Definition immutability
# ---------------------------------------------------------------------------
async def test_definition_is_never_mutated():
    client = FakeHttpClient([FakeResponse(200, body={"candidates": [{}]})])
    node = _node()
    before = node.to_dict()
    await _checker(client).check(node)
    assert node.to_dict() == before


async def test_client_factory_routes_through_node_egress():
    seen = []

    def factory(node):
        seen.append(node.node_id)
        return FakeHttpClient([FakeResponse(200, body={"candidates": []})])

    checker = AnonymousVertexCapabilityChecker(client_factory=factory)
    result = await checker.check(_node())
    assert result.state == NodeAdmissionState.READY
    assert seen == ["node-a"]
