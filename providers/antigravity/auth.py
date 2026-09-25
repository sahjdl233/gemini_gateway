"""Antigravity OAuth token lifecycle (TASK-AUTH-006).

PROTOCOL AUDIT (read-only baseline before this module existed):

* authorization endpoint: NOT part of the gateway.  The Antigravity IDE
  performs the interactive Google OAuth login; the gateway only consumes
  the resulting credential material (``client_id`` / ``client_secret`` /
  ``refresh_token`` on the Resource / Credential).
* token endpoint: no token/refresh code existed in this package before
  AUTH-006 — only static ``access_token`` consumption.  The refresh
  implementation below targets Google's standard OAuth 2.0 token
  endpoint (``https://oauth2.googleapis.com/token``), which is the
  endpoint for the Google OAuth client material this provider carries
  (same credential shape the Code Assist provider in this repo already
  refreshes there — external reference, not copied protocol code).
* refresh request: standard ``grant_type=refresh_token`` form POST with
  ``refresh_token`` / ``client_id`` / ``client_secret``.
* refresh response: ``access_token`` (required), ``expires_in``
  (relative seconds; defaulted when absent), optional ``refresh_token``
  rotation, ``token_type``/``scope`` ignored.
* refresh token rotation: never observed in current code (no refresh
  existed).  Handled opportunistically: a rotated token is kept as
  RUNTIME state on this instance and used for subsequent refreshes, but
  is NEVER written back into the Credential payload or any store until
  AUTH-007/008/009 provide durable persistence.
* expiry: ``expires_in`` (relative) -> runtime ``expires_at = now +
  expires_in``.  The legacy ``Resource.token_expiry`` field is declared
  but was never consumed by any code and remains unconsumed (not
  trusted).

Behaviour preservation: resources configured with only a static
``access_token`` (no refresh material) keep working exactly as before —
the token is used as-is with no expiry and no network calls, and the
Resource field is never mutated.  OAuth refresh activates only when
refresh material is present, which is the new AUTH-006 capability.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from core.errors import AuthenticationError, NetworkError, RateLimitError, TimeoutError

logger = logging.getLogger(__name__)

# Google's standard OAuth 2.0 token endpoint (see protocol audit above).
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# AUTH-006 new behaviour: refresh 3 minutes before expiry (locked by tests).
PRE_REFRESH_SECONDS = 180
# AUTH-006 new behaviour: hard cap on consecutive refresh failures.
MAX_REFRESH_ATTEMPTS = 3
DEFAULT_EXPIRES_IN = 3600.0


class AntigravityAuth:
    """Runtime OAuth access-token lifecycle for one Antigravity resource.

    Scoped to a single resource: the Provider builds one instance (via
    the auth adapter) per Resource, so the token cache and lock are
    resource-scoped even when Resources share a credential_id.
    """

    def __init__(
        self,
        http: Any,
        *,
        token_url: str = GOOGLE_TOKEN_URL,
        clock: Optional[Any] = None,
        material_resolver: Optional[Any] = None,
    ) -> None:
        self._http = http
        self._token_url = token_url
        self._clock = clock if clock is not None else time
        self._lock = asyncio.Lock()
        self._material_resolver = material_resolver
        # Runtime state — never persisted.
        self._access_token: Optional[str] = None
        # None = no known expiry (statically configured token: valid until
        # the upstream rejects it).  float = epoch seconds.
        self._expires_at: Optional[float] = None
        self._rotated_refresh_token: Optional[str] = None
        self._consecutive_refresh_failures = 0

    # -- introspection -------------------------------------------------------

    @property
    def expires_at(self) -> Optional[float]:
        """Epoch seconds when the runtime token expires; None = static."""
        return self._expires_at

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
        if self._expires_at is None:
            return False  # static token: no expiry semantics (pre-AUTH-006)
        return self._now() >= self._expires_at - PRE_REFRESH_SECONDS

    # -- public API ------------------------------------------------------------

    async def get_access_token(
        self,
        resource: Any,
        *,
        force: bool = False,
        material: Optional[dict] = None,
    ) -> Optional[str]:
        """Return a valid access token, refreshing when refresh material
        exists; otherwise serving the statically configured token.

        Returns ``None`` when the resource carries no token material at
        all — pre-AUTH-006 behaviour: the client then sends the request
        without an Authorization header.  ``force=True`` with no refresh
        material raises (the refresh contract must fail honestly).
        """
        async with self._lock:
            material = material or self._material_for(resource)
            if not force and not self._is_expired():
                return self._access_token
            effective_refresh = (
                self._rotated_refresh_token
                or material.get("refresh_token")
                or ""
            )
            if (
                effective_refresh
                and material.get("client_id")
                and material.get("client_secret")
            ):
                return await self._refresh_locked(
                    resource, effective_refresh, material
                )
            if not force and material.get("access_token"):
                # Legacy compat: statically configured token, no lifecycle.
                if self._access_token != material["access_token"]:
                    self._access_token = material["access_token"]
                    self._expires_at = None
                return self._access_token
            if force:
                raise AuthenticationError(
                    "antigravity auth: refresh requires refresh_token, "
                    "client_id and client_secret",
                    provider="antigravity",
                    resource_id=resource.id,
                )
            # No material at all: anonymous request (pre-AUTH-006 behaviour).
            return None

    def invalidate(self) -> None:
        """Drop the runtime access-token cache (never durable material)."""
        self._access_token = None
        self._expires_at = None
        # Rotated refresh token is kept: it is the newest known valid
        # refresh material for this runtime; dropping it would force use
        # of a possibly-revoked older token.

    # -- internals ----------------------------------------------------------------

    def _material_for(self, resource: Any) -> dict:
        if self._material_resolver is not None:
            return self._material_resolver(resource)
        return {
            "refresh_token": getattr(resource, "refresh_token", "") or "",
            "client_id": getattr(resource, "client_id", "") or "",
            "client_secret": getattr(resource, "client_secret", "") or "",
            "access_token": getattr(resource, "access_token", "") or "",
        }

    async def _refresh_locked(
        self,
        resource: Any,
        refresh_token: str,
        material: dict,
    ) -> str:
        if self._consecutive_refresh_failures >= MAX_REFRESH_ATTEMPTS:
            raise AuthenticationError(
                "antigravity auth: refresh already failed "
                + str(MAX_REFRESH_ATTEMPTS)
                + " times; giving up",
                provider="antigravity",
                resource_id=resource.id,
            )
        token, expires_in, rotated = await self._exchange(
            resource, refresh_token, material
        )
        self._access_token = token
        self._expires_at = self._now() + expires_in
        if rotated:
            self._rotated_refresh_token = rotated
        self._consecutive_refresh_failures = 0
        logger.debug("antigravity.auth refreshed resource=%s", resource.id)
        return token

    async def _exchange(
        self,
        resource: Any,
        refresh_token: str,
        material: dict,
    ) -> tuple:
        """POST the refresh_token grant; returns (access_token,
        expires_in, rotated_refresh_token|None)."""
        try:
            resp = await self._http.post(
                self._token_url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "client_id": material["client_id"],
                    "client_secret": material["client_secret"],
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except Exception as exc:  # noqa: BLE001 - transport error
            self._consecutive_refresh_failures += 1
            lowered = str(exc).lower()
            # asyncio.TimeoutError is the builtin TimeoutError alias; the
            # core TimeoutError import above shadows the builtin name.
            if isinstance(exc, asyncio.TimeoutError) or "timeout" in lowered:
                raise TimeoutError(
                    "antigravity auth: refresh transport timeout",
                    provider="antigravity",
                    resource_id=resource.id,
                ) from exc
            raise NetworkError(
                "antigravity auth: refresh transport error",
                provider="antigravity",
                resource_id=resource.id,
            ) from exc

        if resp.status_code != 200:
            self._consecutive_refresh_failures += 1
            if resp.status_code == 429:
                # Rate limiting at the token endpoint is capacity, not an
                # invalid credential — keep rate-limit semantics.
                raise RateLimitError(
                    "antigravity auth: token endpoint rate limited",
                    provider="antigravity",
                    resource_id=resource.id,
                    scope="resource",
                )
            raise AuthenticationError(
                "antigravity auth: refresh failed status="
                + str(resp.status_code),
                provider="antigravity",
                resource_id=resource.id,
            )

        try:
            data = resp.json()
            token = data["access_token"]
        except (KeyError, ValueError, TypeError) as exc:
            self._consecutive_refresh_failures += 1
            raise AuthenticationError(
                "antigravity auth: malformed token response "
                "(missing access_token)",
                provider="antigravity",
                resource_id=resource.id,
            ) from exc
        try:
            expires_in = float(data.get("expires_in", DEFAULT_EXPIRES_IN))
        except (ValueError, TypeError):
            expires_in = DEFAULT_EXPIRES_IN
        rotated = data.get("refresh_token") or None
        return token, expires_in, rotated
