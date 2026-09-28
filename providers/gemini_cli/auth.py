"""OAuth token lifecycle for the Gemini CLI (Code Assist) provider.

Responsibilities (TASK-008):
  - cache the access token with expiry-aware refresh
  - refresh via the oauth2.googleapis.com/token endpoint
  - 401 => force refresh => retry once (handled by the client)
  - never allow infinite refresh loops
  - credentials must never reach logs

The interactive browser OAuth / onboarding flow is intentionally NOT part
of the Provider; it lives in a one-shot setup script (see TASK-007).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from providers.gemini_cli.errors import (
    GeminiCliAuthError,
    GeminiCliNetworkError,
    GeminiCliRateLimitError,
    extract_retry_after,
)

logger = logging.getLogger(__name__)


DEFAULT_TOKEN_URL = "https://oauth2.googleapis.com/token"
PRE_REFRESH_SECONDS = 180  # refresh 3 minutes before expiry (TASK-007:5)
MAX_REFRESH_ATTEMPTS = 3  # hard cap; no infinite refresh


def _raise_refresh_http_error(resp: Any, resource: Any, *, body: Any) -> None:
    """Classify a non-200 token-endpoint response.

    TASK-AUTH-016: a 429 at the TOKEN endpoint is capacity, not a bad
    credential.  It must surface as ``RateLimitError`` (carrying the
    parsed Retry-After) so the Scheduler can cooldown the resource —
    never as ``GeminiCliAuthError`` / ``CredentialRefreshFailure`` / 401.
    Retry-After parsing failure must never degrade the 429 into an auth
    failure: an unparsable header simply yields ``retry_after=None`` and
    the CooldownManager falls back to its own backoff.
    """
    if getattr(resp, "status_code", None) == 429:
        raise GeminiCliRateLimitError(
            "gemini_cli auth: token endpoint rate limited",
            provider="gemini_cli",
            resource_id=resource.id,
            scope="resource",
            retry_after=extract_retry_after(resp),
        )
    raise GeminiCliAuthError(
        "gemini_cli auth: refresh failed status="
        + str(resp.status_code)
        + " body="
        + str(body)[:200],
        provider="gemini_cli",
        resource_id=resource.id,
    )


def resource_material(resource: Any) -> dict:
    """Legacy material source: read OAuth fields straight off the Resource.

    AUTH-002 compatibility path.  When a resource carries ``credential_id``
    the provider binds a resolver that prefers the Credential payload; this
    fallback keeps resources without a Credential working unchanged.
    """
    return {
        "refresh_token": getattr(resource, "refresh_token", "") or "",
        "client_id": getattr(resource, "client_id", "") or "",
        "client_secret": getattr(resource, "client_secret", "") or "",
    }


class GeminiCliAuth:
    """Manages one OAuth access token (single account/resource)."""

    def __init__(
        self,
        http: Any,
        *,
        token_url: str = DEFAULT_TOKEN_URL,
        clock: Optional[Any] = None,
        material_resolver: Optional[Any] = None,
        rotation_listener: Optional[Any] = None,
    ) -> None:
        self._http = http
        self._token_url = token_url
        self._clock = clock or time
        self._lock = asyncio.Lock()
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0
        self._consecutive_refresh_failures = 0
        # Runtime rotation state (AUTH-014): newest refresh token seen in
        # a refresh response, preferred over the resolved material until
        # the durable Credential is updated by the rotation listener.
        self._rotated_refresh_token: Optional[str] = None
        # Optional callable (resource, new_refresh_token) -> None, invoked
        # after a successful refresh that returned a rotated token.
        # Exceptions from the listener propagate: a refresh whose rotated
        # token cannot be persisted fails loudly (never swallowed).
        self._rotation_listener = rotation_listener
        # Optional callable (resource) -> {refresh_token, client_id,
        # client_secret}.  Defaults to the legacy Resource-field source;
        # the provider binds a Credential-aware resolver (AUTH-002).
        self._material_resolver = material_resolver or resource_material

    # -- token expiry -------------------------------------------------------

    def _now(self) -> float:
        now = getattr(self._clock, "time", None)
        if now is not None and callable(now):
            return float(now())
        return float(time.time())

    def _is_expired(self, *, force: bool = False) -> bool:
        if force:
            return True
        if not self._access_token:
            return True
        return self._now() >= self._expires_at - PRE_REFRESH_SECONDS

    # -- public API ---------------------------------------------------------

    @property
    def expires_at(self) -> float:
        """Epoch seconds when the cached access token expires (0 = none)."""
        return self._expires_at

    async def get_access_token(
        self,
        resource: Any,
        *,
        force: bool = False,
        material: Optional[dict] = None,
    ) -> str:
        """Return a valid access token, refreshing it when needed.

        ``material`` optionally supplies the OAuth material explicitly
        (AUTH-004: the auth adapter passes the Credential payload here);
        when omitted the configured material resolver applies, which
        keeps the legacy Resource-field source working unchanged.
        """
        async with self._lock:
            if not force and not self._is_expired():
                return self._access_token  # type: ignore[return-value]
            if self._consecutive_refresh_failures >= MAX_REFRESH_ATTEMPTS:
                raise GeminiCliAuthError(
                    "gemini_cli auth: refresh already failed "
                    + str(MAX_REFRESH_ATTEMPTS)
                    + " times; giving up",
                    provider="gemini_cli",
                    resource_id=resource.id,
                )
            token, expires_in, rotated = await self._refresh(resource, material)
            if rotated and self._rotation_listener is not None:
                # Persist FIRST (AUTH-014-FIX-01): a listener failure must
                # leave NO new runtime state behind — the next call re-runs
                # the refresh instead of reusing an uncommitted token.  Only
                # the refresh_token is durable — never the access token or
                # expiry.
                self._rotation_listener(resource, rotated)
            self._access_token = token
            self._expires_at = self._now() + float(expires_in)
            if rotated:
                self._rotated_refresh_token = rotated
            self._consecutive_refresh_failures = 0
            logger.debug("gemini_cli.auth refreshed resource=%s", resource.id)
            return token

    def invalidate(self) -> None:
        """Drop the cached token (called on 401 before a force refresh).

        The rotated refresh token (runtime rotation state) is kept: it is
        the newest known valid refresh material; dropping it would force
        use of a possibly-revoked older token.
        """
        self._access_token = None
        self._expires_at = 0.0

    def as_dict(self, resource: Any) -> dict:
        """Credential snapshot (client_id/secret/tokens) for onboarding use."""
        material = self._material_resolver(resource)
        return {
            "client_id": material["client_id"],
            "client_secret": material["client_secret"],
            "refresh_token": material["refresh_token"],
            "token": resource.access_token or self._access_token or "",
            "token_uri": self._token_url,
        }

    # -- internals ----------------------------------------------------------

    async def _refresh(self, resource: Any, material: Optional[dict] = None) -> tuple:
        """POST refresh_token grant.

        Returns ``(access_token, expires_in, rotated_refresh_token|None)``.
        A previously rotated token (runtime state) takes precedence over
        the resolved material's refresh_token.  The response's optional
        ``refresh_token`` is captured as rotation.
        """
        material = material or self._material_resolver(resource)
        effective_refresh = self._rotated_refresh_token or material["refresh_token"]
        if not effective_refresh:
            raise GeminiCliAuthError(
                "gemini_cli auth: no refresh_token configured",
                provider="gemini_cli",
                resource_id=resource.id,
            )
        if not material["client_id"] or not material["client_secret"]:
            raise GeminiCliAuthError(
                "gemini_cli auth: client_id/client_secret not configured",
                provider="gemini_cli",
                resource_id=resource.id,
            )
        try:
            resp = await self._http.post(
                self._token_url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "client_id": material["client_id"],
                    "client_secret": material["client_secret"],
                    "refresh_token": effective_refresh,
                    "grant_type": "refresh_token",
                },
            )
        except Exception as exc:  # noqa: BLE001 - transport error
            self._consecutive_refresh_failures += 1
            raise GeminiCliNetworkError(
                "gemini_cli auth: refresh transport error",
                provider="gemini_cli",
                resource_id=resource.id,
            ) from exc

        if resp.status_code != 200:
            body = getattr(resp, "text", "") or getattr(resp, "content", b"")
            if resp.status_code != 429:
                # A 429 is throttling, not a broken credential: it must not
                # consume the MAX_REFRESH_ATTEMPTS give-up budget, or a burst
                # of token-endpoint throttling would permanently disable the
                # credential (the 4th 429 would surface as a 401-class
                # GeminiCliAuthError).  Only genuine refresh failures count.
                self._consecutive_refresh_failures += 1
            _raise_refresh_http_error(
                resp,
                resource,
                body=body,
            )

        try:
            data = resp.json()
            token = data["access_token"]
            expires_in = float(data.get("expires_in", 3600))
        except (KeyError, ValueError, TypeError) as exc:
            self._consecutive_refresh_failures += 1
            raise GeminiCliAuthError(
                "gemini_cli auth: malformed refresh response",
                provider="gemini_cli",
                resource_id=resource.id,
            ) from exc
        rotated = data.get("refresh_token") or None
        return token, expires_in, rotated
