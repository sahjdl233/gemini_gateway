"""Firebase App Check authentication.

Exchanges a Debug Token for a short-lived App Check Provider JWT
via the Firebase App Check REST API. The JWT is cached and refreshed
300 seconds before expiry. On 401 the caller should force-refresh.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from core.errors import ProviderError

from providers.firebase.errors import (
    FirebaseAuthError,
    FirebaseNetworkError,
    FirebaseTimeoutError,
    FirebaseRateLimitError,
    extract_retry_after,
)

logger = logging.getLogger(__name__)


class FirebaseAuth:
    """Manages App Check JWT lifecycle for one FirebaseResource."""

    EXCHANGE_URL = "https://firebaseappcheck.googleapis.com/v1"
    PRE_REFRESH_SECONDS = 300

    def __init__(self, client: Any, *, clock: Optional[Any] = None) -> None:
        self._client = client
        # Optional injectable clock (AUTH-005, tests); defaults to time.time
        # so runtime behaviour is unchanged.
        self._clock = clock if clock is not None else time
        self._jwt: Optional[str] = None
        self._jwt_exp: float = 0.0
        self._lock = asyncio.Lock()

    def _now(self) -> float:
        now = getattr(self._clock, "time", None)
        if now is not None and callable(now):
            return float(now())
        return float(time.time())

    @property
    def expires_at(self) -> float:
        """Epoch seconds when the cached JWT expires (0 = none)."""
        return self._jwt_exp

    async def get_jwt(
        self,
        project_id: str,
        app_id: str,
        api_key: str,
        debug_token: str,
        *,
        force: bool = False,
    ) -> str:
        """Return a valid App Check JWT, refreshing if needed."""
        async with self._lock:
            now = self._now()
            if not force and self._jwt and self._jwt_exp > now + self.PRE_REFRESH_SECONDS:
                return self._jwt
            jwt, exp = await self._exchange(
                project_id, app_id, api_key, debug_token
            )
            self._jwt = jwt
            self._jwt_exp = exp
            logger.debug("firebase.auth refreshed project=%s", project_id)
            return jwt

    async def _exchange(
        self,
        project_id: str,
        app_id: str,
        api_key: str,
        debug_token: str,
    ) -> tuple:
        """POST to exchangeDebugToken and return (jwt, expiry_ts)."""
        url = (
            f"{self.EXCHANGE_URL}/projects/{project_id}"
            f"/apps/{app_id}:exchangeDebugToken?key={api_key}"
        )
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-client": "gl-js/@firebase/ai/2.15.0 fire/2.15.0",
            "x-goog-api-key": api_key,
        }
        body = {"debug_token": debug_token, "limited_use": False}
        try:
            resp = await self._client.post(url, json=body, headers=headers)
        except Exception as exc:
            msg = str(exc)[:200] or "app check exchange network error"
            lowered = msg.lower()
            if "timeout" in lowered or "timed out" in lowered or isinstance(
                exc, asyncio.TimeoutError
            ):
                raise FirebaseTimeoutError(
                    msg, provider="firebase"
                ) from exc
            raise FirebaseNetworkError(msg, provider="firebase") from exc
        if resp.status_code != 200:
            if resp.status_code == 429:
                # TASK-AUTH-016: App Check throttling is capacity, not an
                # invalid debug token.  Surface RateLimitError with the
                # parsed Retry-After so the Scheduler can cooldown.
                # An unparsable/absent header yields retry_after=None and
                # the CooldownManager falls back to its own backoff — a
                # header problem must never become an auth failure.
                raise FirebaseRateLimitError(
                    "App Check exchange rate limited: 429",
                    provider="firebase",
                    scope="resource",
                    retry_after=extract_retry_after(resp),
                )
            raise FirebaseAuthError(
                f"App Check JWT exchange failed: {resp.status_code}",
                provider="firebase",
            )
        data = resp.json()
        token = data["token"]
        ttl_str = str(data.get("ttl", "3600s")).rstrip("s")
        ttl = int(ttl_str) if ttl_str.isdigit() else 3600
        return token, self._now() + ttl

    def invalidate(self) -> None:
        """Force invalidate cached JWT (call on 401)."""
        self._jwt = None
        self._jwt_exp = 0.0
