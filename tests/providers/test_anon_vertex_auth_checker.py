"""ANON-009-C acceptance tests: auth / captcha failure-mapping checker."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from providers.anonymous_vertex.admission import NodeAdmissionState
from providers.anonymous_vertex.admission_policy import (
    AdmissionPolicy,
    DefaultFailureClassifier,
    FailureCategory,
)
from providers.anonymous_vertex.checkers import AnonymousVertexAuthChecker
from providers.anonymous_vertex.checkers.auth import classify_auth_response
from providers.anonymous_vertex.node_definitions import NodeDefinition

_PROBE_PATH = "/v1internal:generateContent"


class FakeResponse:
    def __init__(self, status_code=200, body=None, raw=None):
        self.status_code = status_code
        self._body = body
        self._raw = raw

    def json(self):
        if self._raw is not None:
            raise json.JSONDecodeError("not json", self._raw, 0)
        return self._body


class FakeHttpClient:
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
    return AnonymousVertexAuthChecker(client, **kwargs)


# ---------------------------------------------------------------------------
# Anonymous success
# ---------------------------------------------------------------------------
async def test_anonymous_success_is_ready():
    client = FakeHttpClient([FakeResponse(200, body={"candidates": []})])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.READY
    assert result.reason is None
    (call,) = client.calls
    assert call["url"] == "https://162.159.198.1:443" + _PROBE_PATH
    assert call["json"]["contents"][0]["parts"][0]["text"] == "ping"


# ---------------------------------------------------------------------------
# Auth failures
# ---------------------------------------------------------------------------
async def test_http_401_is_auth_failed():
    client = FakeHttpClient([FakeResponse(401, body={"error": {"message": "no"}})])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.FAILED
    assert result.reason == "auth_failed"


async def test_unauthenticated_status_is_auth_failed_even_on_http_200():
    body = {"error": {"status": "UNAUTHENTICATED", "message": "x"}}
    client = FakeHttpClient([FakeResponse(200, body=body)])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.FAILED
    assert result.reason == "auth_failed"


async def test_permission_denied_status_is_auth_failed():
    body = {"error": {"status": "PERMISSION_DENIED", "message": "x"}}
    client = FakeHttpClient([FakeResponse(403, body=body)])
    result = await _checker(client).check(_node())
    assert result.reason == "auth_failed"


async def test_plain_403_without_marker_keeps_status_token():
    """A bare 403 must NOT be misread as an auth failure (spec section 6)."""
    client = FakeHttpClient([FakeResponse(403, body={"error": {"message": "nope"}})])
    result = await _checker(client).check(_node())
    assert result.reason == "http_status_403"


# ---------------------------------------------------------------------------
# CAPTCHA / risk signals
# ---------------------------------------------------------------------------
async def test_captcha_message_is_captcha_required():
    body = {"error": {"message": "captcha required"}}
    client = FakeHttpClient([FakeResponse(200, body=body)])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.FAILED
    assert result.reason == "captcha_required"


async def test_risk_signal_is_captcha_required():
    body = {"error": {"message": "request blocked by risk control"}}
    client = FakeHttpClient([FakeResponse(403, body=body)])
    result = await _checker(client).check(_node())
    assert result.reason == "captcha_required"


async def test_recaptcha_signal_detected():
    body = {"error": {"message": "recaptcha verification failed"}}
    client = FakeHttpClient([FakeResponse(200, body=body)])
    result = await _checker(client).check(_node())
    assert result.reason == "captcha_required"


# ---------------------------------------------------------------------------
# Generic HTTP / transport failures
# ---------------------------------------------------------------------------
async def test_server_error_500_keeps_status_token():
    client = FakeHttpClient([FakeResponse(500, raw="boom")])
    result = await _checker(client).check(_node())
    assert result.reason == "http_status_500"


async def test_timeout_maps_to_timeout_reason():
    client = FakeHttpClient([asyncio.TimeoutError()])
    assert (await _checker(client).check(_node())).reason == "timeout"
    client = FakeHttpClient([httpx.ReadTimeout("timed out")])
    assert (await _checker(client).check(_node())).reason == "timeout"


async def test_network_error_maps_to_network_error_reason():
    client = FakeHttpClient([httpx.ConnectError("connection reset")])
    result = await _checker(client).check(_node())
    assert result.state == NodeAdmissionState.FAILED
    assert result.reason == "network_error"


async def test_non_json_success_body_is_invalid_response():
    client = FakeHttpClient([FakeResponse(200, raw="<html>hi</html>")])
    result = await _checker(client).check(_node())
    assert result.reason == "invalid_response"


async def test_cancellation_propagates():
    class ParkedClient:
        async def post(self, url, json=None, headers=None, timeout=None):
            await asyncio.sleep(30.0)

    task = asyncio.create_task(_checker(ParkedClient()).check(_node()))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# No credential leakage
# ---------------------------------------------------------------------------
async def test_request_carries_no_credential_material():
    client = FakeHttpClient([FakeResponse(200, body={"candidates": []})])
    await _checker(client).check(_node())
    (call,) = client.calls
    headers = {k.lower(): v for k, v in (call["headers"] or {}).items()}
    assert "content-type" in headers
    for forbidden in ("authorization", "cookie", "x-goog-user-project"):
        assert forbidden not in headers
        assert not any(forbidden in k for k in headers), forbidden


async def test_definition_is_never_mutated():
    client = FakeHttpClient([FakeResponse(200, body={"candidates": []})])
    node = _node()
    before = node.to_dict()
    await _checker(client).check(node)
    assert node.to_dict() == before


# ---------------------------------------------------------------------------
# classify_auth_response: pure function contract
# ---------------------------------------------------------------------------
def test_classify_auth_response_is_pure():
    payload = {"error": {"status": "UNAUTHENTICATED"}}
    snapshot = json.dumps(payload)
    assert classify_auth_response(200, payload) == "auth_failed"
    assert json.dumps(payload) == snapshot  # payload untouched
    assert classify_auth_response(401, None) == "auth_failed"
    assert classify_auth_response(500, {"error": {"message": "oops"}}) is None
    assert classify_auth_response(200, {"candidates": []}) is None


# ---------------------------------------------------------------------------
# Pipeline integration: captcha_required -> QUARANTINED, reason preserved
# ---------------------------------------------------------------------------
async def test_captcha_result_flows_to_quarantine_through_policy():
    classifier = DefaultFailureClassifier()
    policy = AdmissionPolicy(classifier=classifier)

    client = FakeHttpClient(
        [FakeResponse(200, body={"error": {"message": "captcha required"}})]
    )
    result = await _checker(client).check(_node())
    assert result.reason == "captcha_required"

    assert classifier.classify(result) == FailureCategory.CAPTCHA
    final = policy.decide(result)
    assert final.state == NodeAdmissionState.QUARANTINED
    assert final.reason == "captcha_required"  # reason preserved end-to-end
    assert final.checked_at == result.checked_at


async def test_auth_result_flows_to_quarantine_through_policy():
    classifier = DefaultFailureClassifier()
    policy = AdmissionPolicy(classifier=classifier)

    client = FakeHttpClient([FakeResponse(401, body={})])
    result = await _checker(client).check(_node())
    assert result.reason == "auth_failed"

    assert classifier.classify(result) == FailureCategory.AUTH
    final = policy.decide(result)
    assert final.state == NodeAdmissionState.QUARANTINED
    assert final.reason == "auth_failed"
