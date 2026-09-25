"""GeminiCliAuthAdapter — AUTH-003 contract implementation (AUTH-004).

The formal owner of the Gemini CLI OAuth lifecycle::

    Resource.credential_id
        ↓
    CredentialStore ──→ Credential(type=oauth)
        ↓
    GeminiCliAuthAdapter          (this module: contract + material source)
        ↓
    GeminiCliAuth                 (the single OAuth implementation:
        ↓                          cache / 180s pre-refresh / refresh /
    RuntimeCredentials             401 retry support / failure cap)
        ↓
    GeminiCliClient → Authorization: Bearer <access token>

Boundaries frozen by AUTH-004:

* There is exactly ONE OAuth protocol implementation (``GeminiCliAuth``).
  The adapter wraps it; nothing else re-implements refresh.
* The Credential (type=oauth) canonically owns ``refresh_token``,
  ``client_id`` and ``client_secret``.  Resource legacy fields remain a
  compatibility source only.  Precedence: explicit credential >
  resource.credential_id store lookup > legacy Resource fields.
* The access token is RUNTIME material only.  The adapter never writes
  it back into the Credential payload or any store; ``invalidate()``
  drops the runtime cache and never touches durable material.
* Runtime cache scope is RESOURCE-scoped: the Provider builds one
  adapter (and therefore one token cache + lock) per Resource, even when
  two Resources share the same credential_id.  Credential sharing does
  not imply runtime cache sharing.
* Failure mapping: refresh protocol failure raises
  ``CredentialRefreshFailure`` (the contract form of the existing
  ``GeminiCliAuthError``, itself a core ``AuthenticationError``).
  Transport errors during refresh stay ``GeminiCliNetworkError`` — a
  network problem is not a credential failure and must stay retryable.
"""

from __future__ import annotations

from typing import Any, Optional

from core.auth_adapter import (
    CredentialRefreshFailure,
    CredentialUnavailableError,
    InvalidCredentialError,
    ProviderAuthAdapter,
    RuntimeCredentials,
    require_bound_credential,
)
from core.credential import Credential, CredentialStore, CredentialType
from core.resource import Resource

from providers.gemini_cli.auth import DEFAULT_TOKEN_URL, GeminiCliAuth, resource_material
from providers.gemini_cli.errors import GeminiCliAuthError

# Material the Code Assist OAuth flow requires (GeminiCliAuth._refresh
# rejects credentials without these — current code is the source of truth).
REQUIRED_OAUTH_FIELDS = ("refresh_token", "client_id", "client_secret")


class GeminiCliAuthAdapter(ProviderAuthAdapter):
    """ProviderAuthAdapter for the Gemini CLI (Code Assist) OAuth protocol."""

    provider_id = "gemini_cli"
    credential_types = frozenset({CredentialType.OAUTH})

    def __init__(
        self,
        *,
        http: Any,
        credential_store: Optional[CredentialStore] = None,
        token_url: str = DEFAULT_TOKEN_URL,
        clock: Optional[Any] = None,
    ) -> None:
        self._credential_store = credential_store
        # The adapter owns the OAuth implementation instance; the token
        # cache/lock lives here, scoped to one Resource (the Provider
        # builds one adapter per resource).
        self._auth = GeminiCliAuth(
            http=http,
            token_url=token_url,
            clock=clock,
            material_resolver=self.material_for,
        )

    @property
    def auth(self) -> GeminiCliAuth:
        """The wrapped OAuth implementation (used by GeminiCliClient)."""
        return self._auth

    # -- material resolution (single source of truth) ------------------------

    def material_for(self, resource: Any) -> dict:
        """Resolve OAuth material for a Resource.

        Strict reference integrity (AUTH-013): a set ``credential_id``
        must resolve to an existing oauth Credential — a missing or
        mistyped credential raises CredentialUnavailableError instead of
        silently falling back to the legacy Resource fields.  Legacy
        fields apply only when ``credential_id`` is None.
        """
        if not resource.credential_id:
            return resource_material(resource)
        credential = require_bound_credential(
            resource.credential_id,
            resource,
            store=self._credential_store,
            expected_type=CredentialType.OAUTH,
            provider_id=self.provider_id,
        )
        return self.material_from(credential, resource)

    def material_from(
        self,
        credential: Optional[Credential],
        resource: Any,
    ) -> dict:
        """Material from an explicit credential, falling back to legacy
        Resource fields when none applies."""
        if credential is not None and credential.type is CredentialType.OAUTH:
            payload = credential.payload
            return {
                field: str(payload.get(field) or "")
                for field in REQUIRED_OAUTH_FIELDS
            }
        return resource_material(resource)

    # -- AUTH-003 contract ----------------------------------------------------

    async def validate(
        self,
        credential: Credential,
        resource: Optional[Resource] = None,
    ) -> None:
        """Check the Credential carries the material this OAuth flow needs.

        No network I/O; exception messages name missing fields only.
        """
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

        Serves the cached token while it is valid (180s pre-refresh
        window honoured by the wrapped implementation) and refreshes
        when near expiry or absent.
        """
        self.ensure_supported(credential, resource)
        if resource is None:
            raise CredentialUnavailableError(
                "gemini_cli auth requires a resource context",
                provider=self.provider_id,
            )
        material = self.material_from(credential, resource)
        token = await self._auth.get_access_token(resource, material=material)
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
        """Force-run the OAuth refresh protocol (failure-capped by the
        implementation: MAX_REFRESH_ATTEMPTS)."""
        self.ensure_supported(credential, resource)
        if resource is None:
            raise CredentialUnavailableError(
                "gemini_cli auth requires a resource context",
                provider=self.provider_id,
            )
        material = self.material_from(credential, resource)
        try:
            token = await self._auth.get_access_token(
                resource, force=True, material=material
            )
        except GeminiCliAuthError as exc:
            # Contract form of the existing auth failure.  The message
            # names the failure mode only — no tokens or secrets (the
            # original error remains chained as __cause__ for debugging).
            raise CredentialRefreshFailure(
                f"gemini_cli auth: refresh failed ({type(exc).__name__})",
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

        This is NOT a credential delete: durable material in the
        Credential payload is untouched and the next
        get_runtime_credentials() call refreshes/reacquires.
        """
        self._auth.invalidate()
