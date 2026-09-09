"""Error hierarchy tests (TASK-000 rule 8)."""

from __future__ import annotations

from core.errors import (
    AuthenticationError,
    AuthorizationError,
    ContentFilterError,
    InvalidRequestError,
    ModelNotFoundError,
    NetworkError,
    ProtocolError,
    ProviderError,
    RateLimitError,
    TimeoutError,
    UnknownProviderError,
    UpstreamUnavailableError,
    is_retryable,
    provider_error_status,
)


def test_hierarchy():
    for cls in (
        AuthenticationError,
        AuthorizationError,
        RateLimitError,
        InvalidRequestError,
        ModelNotFoundError,
        ContentFilterError,
        UpstreamUnavailableError,
        NetworkError,
        TimeoutError,
        ProtocolError,
        UnknownProviderError,
    ):
        assert issubclass(cls, ProviderError)


def test_rate_limit_is_first_class():
    err = RateLimitError(
        "too many requests",
        provider="fake",
        resource_id="res-1",
        scope="project",
        retry_after=7.5,
    )
    assert err.provider == "fake"
    assert err.resource_id == "res-1"
    assert err.scope == "project"
    assert err.retry_after == 7.5
    assert provider_error_status(err) == 429


def test_retryable_classification():
    assert is_retryable(RateLimitError("x"))
    assert is_retryable(TimeoutError("x"))
    assert is_retryable(NetworkError("x"))
    assert is_retryable(UpstreamUnavailableError("x"))
    assert not is_retryable(AuthenticationError("x"))
    assert not is_retryable(ModelNotFoundError("x"))
    assert not is_retryable(UnknownProviderError("x"))


def test_status_mapping():
    assert provider_error_status(AuthenticationError("x")) == 401
    assert provider_error_status(AuthorizationError("x")) == 403
    assert provider_error_status(InvalidRequestError("x")) == 400
    assert provider_error_status(ModelNotFoundError("x")) == 404
    assert provider_error_status(ContentFilterError("x")) == 400
    assert provider_error_status(TimeoutError("x")) == 504
    assert provider_error_status(NetworkError("x")) == 502
    assert provider_error_status(UpstreamUnavailableError("x")) == 503
    assert provider_error_status(ProtocolError("x")) == 502
    assert provider_error_status(UnknownProviderError("x")) == 500


def test_default_fields():
    err = ProviderError("boom")
    assert err.provider == "unknown"
    assert err.resource_id is None
    assert err.scope == "unknown"
    assert err.retry_after is None
