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

from providers.gemini_cli.errors import GeminiCliAuthError, GeminiCliNetworkError

logger = logging.getLogger(__name__)


DEFAULT_TOKEN_URL = "https://oauth2.googleapis.com/token"
PRE_REFRESH_SECONDS = 180  # refresh 3 minutes before expiry (TASK-007:5)
MAX_REFRESH_ATTEMPTS = 3  # hard cap; no infinite refresh


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
    ) -> None:
        self._http = http
        self._token_url = token_url
        self._clock = clock or time
        self._lock = asyncio.Lock()
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0
        self._consecutive_refresh_failures = 0
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

    async def get_access_token(
        self,
        resource: Any,
        *,
        force: bool = False,
    ) -> str:
        """Return a valid access token, refreshing it when needed."""
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
            token, expires_in = await self._refresh(resource)
            self._access_token = token
            self._expires_at = self._now() + float(expires_in)
            self._consecutive_refresh_failures = 0
            logger.debug("gemini_cli.auth refreshed resource=%s", resource.id)
            return token

    def invalidate(self) -> None:
        """Drop the cached token (called on 401 before a force refresh)."""
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

    async def _refresh(self, resource: Any) -> tuple:
        """POST refresh_token grant; returns (access_token, expires_in)."""
        material = self._material_resolver(resource)
        if not material["refresh_token"]:
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
                    "refresh_token": material["refresh_token"],
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
            self._consecutive_refresh_failures += 1
            body = getattr(resp, "text", "") or getattr(resp, "content", b"")
            raise GeminiCliAuthError(
                "gemini_cli auth: refresh failed status="
                + str(resp.status_code)
                + " body="
                + str(body)[:200],
                provider="gemini_cli",
                resource_id=resource.id,
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
        return token, expires_in
