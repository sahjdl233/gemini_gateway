"""Error mapping: 401/403/404/429/5xx + 429 reset time formats (TASK-008)."""
from __future__ import annotations

import time

import pytest

from core.errors import (
    AuthenticationError,
    AuthorizationError,
    ModelNotFoundError,
    RateLimitError,
    UpstreamUnavailableError,
    NetworkError,
    TimeoutError,
    InvalidRequestError,
)
from providers.gemini_cli.errors import (
    classify_http_error,
    classify_transport_error,
    parse_reset_seconds,
    default_reset_seconds,
    extract_retry_after,
    GeminiCliRateLimitError,
)


class FakeResp:
    def __init__(self, status_code, json_body=None, headers=None):
        self.status_code = status_code
        self._json = json_body
        self.headers = headers or {}

    def json(self):
        return self._json


def test_classify_401():
    exc = classify_http_error(401, {}, resource_id="r1")
    assert isinstance(exc, AuthenticationError)
    assert exc.scope == "account"
    assert exc.resource_id == "r1"


def test_classify_403():
    exc = classify_http_error(403, {}, resource_id="r2")
    assert isinstance(exc, AuthorizationError)
    assert exc.scope == "resource"


def test_classify_404():
    exc = classify_http_error(404, {}, resource_id="r3")
    assert isinstance(exc, ModelNotFoundError)


def test_classify_500_503():
    for code in (500, 502, 503):
        exc = classify_http_error(code, {}, resource_id="r")
        assert isinstance(exc, UpstreamUnavailableError)


def test_classify_timeout():
    import httpx
    exc = classify_transport_error(httpx.TimeoutException("boom"), resource_id="r")
    assert isinstance(exc, TimeoutError)


def test_classify_network():
    exc = classify_transport_error(ConnectionError("fail"), resource_id="r")
    assert isinstance(exc, NetworkError)


def test_429_quota_reset_timestamp_iso8601():
    body = {
        "error": {
            "details": [
                {
                    "metadata": {
                        "quotaResetTimeStamp": "2099-01-01T00:00:00Z"
                    }
                }
            ]
        }
    }
    now = 1700000000.0
    secs = parse_reset_seconds(429, body, now=now)
    assert secs is not None
    assert secs > 0


def test_429_quota_reset_delay_gostyle():
    body = {
        "error": {
            "details": [
                {
                    "metadata": {
                        "quotaResetDelay": "13h19m1.20964964s"
                    }
                }
            ]
        }
    }
    secs = parse_reset_seconds(429, body)
    assert secs is not None
    expected = 13 * 3600 + 19 * 60 + 1.20964964
    assert abs(secs - expected) < 0.01


def test_429_text_form():
    body = {
        "error": {
            "message": "Your quota will reset after 6h 30m 15s.",
        }
    }
    secs = parse_reset_seconds(429, body)
    assert secs is not None
    expected = 6 * 3600 + 30 * 60 + 15
    assert abs(secs - expected) < 0.01


def test_429_fallback_4h():
    body = {"error": {"message": "RESOURCE_EXHAUSTED"}}
    secs = parse_reset_seconds(429, body)
    assert secs is None
    # default 4h
    assert default_reset_seconds() == 4 * 3600


def test_429_rate_limit_error_has_retry_after():
    body = {
        "error": {
            "details": [
                {"metadata": {"quotaResetDelay": "1h0m0s"}}
            ]
        }
    }
    exc = classify_http_error(429, body, resource_id="r")
    assert isinstance(exc, RateLimitError)
    assert exc.retry_after is not None
    assert abs(exc.retry_after - 3600) < 1


def test_extract_retry_after_header():
    class R:
        headers = {"Retry-After": "120"}
    secs = extract_retry_after(R())
    assert secs == 120.0


def test_extract_retry_after_missing():
    class R:
        headers = {}
    assert extract_retry_after(R()) is None


def test_non_429_returns_none():
    assert parse_reset_seconds(401, {}) is None
    assert parse_reset_seconds(500, {}) is None

