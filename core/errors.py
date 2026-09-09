"""Unified provider error hierarchy.

Every Provider Adapter MUST translate upstream errors into these types.
The Scheduler is forbidden from parsing raw Google error strings.
"""

from __future__ import annotations

from typing import Optional


class ProviderError(Exception):
    """Base class for every error surfaced by a Provider Adapter."""

    default_status = 500

    def __init__(
        self,
        message: str,
        *,
        provider: str = "unknown",
        resource_id: Optional[str] = None,
        scope: str = "unknown",
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.resource_id = resource_id
        self.scope = scope
        self.retry_after = retry_after


class AuthenticationError(ProviderError):
    """Invalid / missing credentials (upstream 401)."""

    default_status = 401


class AuthorizationError(ProviderError):
    """Authenticated but not allowed (upstream 403)."""

    default_status = 403


class RateLimitError(ProviderError):
    """A rate limit was hit (upstream 429).

    'scope' tells the scheduler WHAT is limited (resource / account /
    project / egress / unknown) so it can decide the right cooldown level.
    """

    default_status = 429


class InvalidRequestError(ProviderError):
    """The request itself is malformed / rejected upstream (400)."""

    default_status = 400


class ModelNotFoundError(ProviderError):
    """The requested model is unknown (404)."""

    default_status = 404


class ContentFilterError(ProviderError):
    """Upstream blocked the content (safety filter)."""

    default_status = 400


class UpstreamUnavailableError(ProviderError):
    """Upstream service is down (5xx gateway)."""

    default_status = 503


class NetworkError(ProviderError):
    """Connection-level failure (bad gateway)."""

    default_status = 502


class TimeoutError(ProviderError):
    """Upstream did not answer in time (gateway timeout)."""

    default_status = 504


class ProtocolError(ProviderError):
    """Upstream answered with malformed / unexpected data."""

    default_status = 502


class UnknownProviderError(ProviderError):
    """Anything the adapter could not classify."""

    default_status = 500


_RETRYABLE = (
    RateLimitError,
    UpstreamUnavailableError,
    NetworkError,
    TimeoutError,
)


def is_retryable(error: ProviderError) -> bool:
    """True when the Scheduler may retry this error on another resource."""
    return isinstance(error, _RETRYABLE)


def provider_error_status(error: ProviderError) -> int:
    """HTTP status the OpenAI-compatible layer should expose for 'error'."""
    return getattr(error, "default_status", 500)
