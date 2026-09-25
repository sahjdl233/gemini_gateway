"""AntigravityAuthAdapter — AUTH-003 contract implementation (AUTH-006).

The formal owner of the Antigravity OAuth lifecycle::

    Resource.credential_id
        ↓
    CredentialStore ──→ Credential(type=oauth)
        ↓
    AntigravityAuthAdapter        (this module: contract + material source)
        ↓
    AntigravityAuth               (the single OAuth implementation:
        ↓                          token cache / 180s pre-refresh /
    RuntimeCredentials             refresh_token grant / rotation state)
        ↓
    AntigravityClient → Authorization: Bearer <access token>
        ↓
    ExecutionBackend

Credential mapping (canonical vs compatibility):

* ``Credential(type=oauth)`` canonically owns ``refresh_token`` /
  ``client_id`` / ``client_secret``.  ``validate`` requires exactly
  these.
* ``access_token`` is RUNTIME material.  AUTH-002's compatibility
  mapping allowed an access_token in the credential payload and on the
  legacy Resource; both remain supported as a STATIC token source
  (pre-AUTH-006 behaviour: used as-is, no expiry, no refresh), but a
  refreshed token never replaces them — new tokens live only in
  ``AntigravityAuth`` runtime state.
* Precedence: explicit credential > credential_id store lookup > legacy
  Resource fields.

Cache scope: RESOURCE-scoped (one adapter / token cache / lock per
Resource, even when Resources share a credential_id).

Failure mapping (all ProviderErrors from core.errors):

* refresh credential/material problems -> ``CredentialRefreshFailure``
  (contract form of the core ``AuthenticationError``)
* token endpoint 429 -> ``RateLimitError`` (rate-limit semantics, NEVER
  a credential failure)
* transport errors -> ``NetworkError`` / ``TimeoutError`` (retryable)
"""

from __future__ import annotations

from typing import Any, Optional

from core.auth_adapter import (
    CredentialRefreshFailure,
    CredentialUnavailableError,
    InvalidCredentialError,
    ProviderAuthAdapter,
    RuntimeCredentials,
)
from core.credential import Credential, CredentialStore, CredentialType
from core.errors import (
    AuthenticationError,
    NetworkError,
    RateLimitError,
    TimeoutError,
)
from core.resource import Resource

from providers.antigravity.auth import AntigravityAuth

# Material the refresh_token grant requires (section 11: validate checks
# these; static-token-only credentials remain usable through the
# compatibility path without passing validate).
REQUIRED_OAUTH_FIELDS = ("refresh_token", "client_id", "client_secret")


class AntigravityAuthAdapter(ProviderAuthAdapter):
    """ProviderAuthAdapter for the Antigravity Google OAuth protocol."""

    provider_id = "antigravity"
    credential_types = frozenset({CredentialType.OAUTH})

    def __init__(
        self,
        *,
        http: Any,
        credential_store: Optional[CredentialStore] = None,
        token_url: Optional[str] = None,
        clock: Optional[Any] = None,
    ) -> None:
        self._credential_store = credential_store
        kwargs: dict = {"http": http, "material_resolver": self.material_for}
        if token_url is not None:
            kwargs["token_url"] = token_url
        if clock is not None:
            kwargs["clock"] = clock
        # The adapter owns the OAuth implementation instance; the token
        # cache / lock / rotation state live here, scoped to one Resource.
        self._auth = AntigravityAuth(**kwargs)

    @property
    def auth(self) -> AntigravityAuth:
        """The wrapped OAuth implementation."""
        return self._auth

    # -- material resolution (single source of truth) ------------------------

    def material_for(self, resource: Any) -> dict:
        """Resolve OAuth material for a Resource.

        Canonical: ``resource.credential_id`` → Credential(type=oauth)
        payload.  Compatibility: legacy Resource fields.  ``access_token``
        is carried as a STATIC token source only (compat), never as the
        target of refresh writes.
        """
        credential = None
        if self._credential_store is not None and resource.credential_id:
            credential = self._credential_store.get(resource.credential_id)
        return self.material_from(credential, resource)

    def material_from(
        self,
        credential: Optional[Credential],
        resource: Any,
    ) -> dict:
        """Material from an explicit credential, falling back to legacy
        Resource fields per field when no credential applies."""
        if credential is not None and credential.type is CredentialType.OAUTH:
            payload = credential.payload
            return {
                "refresh_token": str(payload.get("refresh_token") or ""),
                "client_id": str(payload.get("client_id") or ""),
                "client_secret": str(payload.get("client_secret") or ""),
                # AUTH-002 compat: a static access_token in the payload is
                # still honoured as a runtime seed, never persisted back.
                "access_token": str(payload.get("access_token") or ""),
            }
        return {
            "refresh_token": getattr(resource, "refresh_token", "") or "",
            "client_id": getattr(resource, "client_id", "") or "",
            "client_secret": getattr(resource, "client_secret", "") or "",
            "access_token": getattr(resource, "access_token", "") or "",
        }

    # -- AUTH-003 contract ----------------------------------------------------

    async def validate(
        self,
        credential: Credential,
        resource: Optional[Resource] = None,
    ) -> None:
        """Check the Credential carries the refresh material the OAuth
        grant needs.  No network I/O; messages name fields only."""
        self.ensure_supported(credential, resource)
        missing = [
            field
            for field in REQUIRED_OAUTH_FIELDS
            if not str(credential.payload.get(field) or "")
        ]
        if missing:
            raise InvalidCredentialError(
                f"oauth credential '{credential.id}' missing required "
                f"material: {', '.join(missing)}",
                provider=self.provider_id,
                resource_id=resource.id if resource is not None else None,
            )

    async def get_runtime_credentials(
        self,
        credential: Credential,
        resource: Optional[Resource] = None,
    ) -> RuntimeCredentials:
        """Return a valid Bearer Authorization header.

        Serves the cached token while valid (180s pre-refresh window for
        refreshed tokens; static tokens have no expiry), refreshing when
        refresh material exists.
        """
        self.ensure_supported(credential, resource)
        if resource is None:
            raise CredentialUnavailableError(
                "antigravity auth requires a resource context",
                provider=self.provider_id,
            )
        material = self.material_from(credential, resource)
        token = await self._auth.get_access_token(resource, material=material)
        if token is None:
            raise CredentialUnavailableError(
                f"credential '{credential.id}' carries no usable auth material",
                provider=self.provider_id,
                resource_id=resource.id,
            )
        return RuntimeCredentials(
            headers={"Authorization": "Bearer " + token},
            metadata={},
            expires_at=self._auth.expires_at,
        )

    async def refresh(
        self,
        credential: Credential,
        resource: Optional[Resource] = None,
    ) -> RuntimeCredentials:
        """Force-run the OAuth refresh_token grant.

        Failures map to ``CredentialRefreshFailure`` only for genuine
        authentication/material semantics; network, timeout and rate
        limit keep their own retryable semantics.
        """
        self.ensure_supported(credential, resource)
        if resource is None:
            raise CredentialUnavailableError(
                "antigravity auth requires a resource context",
                provider=self.provider_id,
            )
        material = self.material_from(credential, resource)
        try:
            token = await self._auth.get_access_token(
                resource, force=True, material=material
            )
        except RateLimitError:
            raise  # token endpoint throttling: rate-limit semantics
        except (NetworkError, TimeoutError):
            raise  # transport problems: retryable, not credential failures
        except AuthenticationError as exc:
            raise CredentialRefreshFailure(
                f"antigravity auth: refresh failed ({type(exc).__name__})",
                provider=self.provider_id,
                resource_id=resource.id,
            ) from exc
        return RuntimeCredentials(
            headers={"Authorization": "Bearer " + token},
            metadata={},
            expires_at=self._auth.expires_at,
        )

    async def invalidate(
        self,
        credential: Credential,
        resource: Optional[Resource] = None,
    ) -> None:
        """Drop the runtime access-token cache.

        This is NOT a credential delete: durable material (and the
        runtime rotated-refresh-token state) is untouched; the next
        get_runtime_credentials() call refreshes or re-seeds.
        """
        self._auth.invalidate()
