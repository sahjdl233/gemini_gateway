"""FirebaseAuthAdapter — AUTH-003 contract implementation (AUTH-005).

The formal owner of the Firebase App Check credential lifecycle::

    Resource.credential_id (+ Resource.project_id as identity context)
        ↓
    CredentialStore ──→ Credential(type=api_key)
        ↓
    FirebaseAuthAdapter           (this module: contract + material source)
        ↓
    FirebaseAuth                  (the single App Check implementation:
        ↓                          debug_token exchange / 300s pre-refresh /
    RuntimeCredentials             JWT cache / asyncio.Lock single-flight)
        ↓
    FirebaseClient → firebasevertexai.googleapis.com

Firebase is NOT OAuth: the "refresh" protocol is a debug_token → App
Check JWT exchange against firebaseappcheck.googleapis.com.  The adapter
deliberately keeps Firebase terminology — no refresh_token grant, no
OAuth concepts leak into this provider.

Boundaries frozen by AUTH-005:

* Exactly ONE App Check exchange implementation (``FirebaseAuth``).  The
  adapter wraps it; nothing else re-implements the protocol.
* Credential(type=api_key) canonically owns ``api_key`` / ``app_id`` /
  ``debug_token`` (the AUTH-002 mapping).  ``Resource.project_id``
  always wins over any payload project_id — the project is Resource
  identity (one Firebase Project = one Resource).
* The App Check JWT is RUNTIME material only.  The adapter never writes
  it back into the Credential payload or any store; ``invalidate()``
  drops the JWT cache and never touches durable material.
* Runtime cache scope is RESOURCE-scoped: the Provider builds one
  adapter (and therefore one JWT cache + lock) per Resource, even when
  Resources share a credential_id.  Credential sharing does not imply
  runtime cache sharing.
* Failure mapping: exchange auth failure (``FirebaseAuthError``, itself
  a core ``AuthenticationError``) raises ``CredentialRefreshFailure``
  on the contract surface.  Network/timeout errors stay
  ``FirebaseNetworkError``/``FirebaseTimeoutError`` — a transport
  problem is not a credential failure and must stay retryable.  API
  endpoint 429 stays rate-limit semantics and is never a credential
  failure.
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

from providers.firebase.auth import FirebaseAuth
from providers.firebase.client import GOOG_API_CLIENT, resource_material
from providers.firebase.errors import (
    FirebaseNetworkError,
    FirebaseAuthError,
    FirebaseTimeoutError,
)

# Durable material the App Check exchange requires (FirebaseAuth._exchange
# sends these; current code is the source of truth).  project_id is
# Resource context, not credential material.
REQUIRED_FIREBASE_FIELDS = ("api_key", "app_id", "debug_token")


class FirebaseAuthAdapter(ProviderAuthAdapter):
    """ProviderAuthAdapter for the Firebase App Check protocol."""

    provider_id = "firebase"
    credential_types = frozenset({CredentialType.API_KEY})

    def __init__(
        self,
        *,
        http: Any,
        credential_store: Optional[CredentialStore] = None,
        clock: Optional[Any] = None,
    ) -> None:
        self._credential_store = credential_store
        # The adapter owns the App Check implementation instance; the JWT
        # cache/lock lives here, scoped to one Resource (the Provider
        # builds one adapter per resource).
        self._auth = FirebaseAuth(client=http, clock=clock)

    @property
    def auth(self) -> FirebaseAuth:
        """The wrapped App Check implementation (used by FirebaseClient)."""
        return self._auth

    # -- material resolution (single source of truth) ------------------------

    def material_for(self, resource: Any) -> dict:
        """Resolve project credentials for a Resource.

        Strict reference integrity (AUTH-013): a set ``credential_id``
        must resolve to an existing api_key Credential — a missing or
        mistyped credential raises CredentialUnavailableError instead of
        silently falling back to the legacy Resource fields.  Legacy
        fields apply only when ``credential_id`` is None.
        ``project_id`` is Resource identity; a payload project_id only
        fills the gap when the Resource has none.
        """
        if not resource.credential_id:
            return resource_material(resource)
        credential = require_bound_credential(
            resource.credential_id,
            resource,
            store=self._credential_store,
            expected_type=CredentialType.API_KEY,
            provider_id=self.provider_id,
        )
        return self.material_from(credential, resource)

    def material_from(
        self,
        credential: Optional[Credential],
        resource: Any,
    ) -> dict:
        """Material from an explicit credential, falling back to legacy
        Resource fields per field when no credential applies."""
        if credential is not None and credential.type is CredentialType.API_KEY:
            payload = credential.payload
            return {
                "project_id": (
                    resource.project_id or str(payload.get("project_id") or "")
                ),
                "app_id": str(payload.get("app_id") or resource.app_id or ""),
                "api_key": str(payload.get("api_key") or resource.api_key or ""),
                "debug_token": str(
                    payload.get("debug_token") or resource.debug_token or ""
                ),
            }
        return resource_material(resource)

    # -- AUTH-003 contract ----------------------------------------------------

    async def validate(
        self,
        credential: Credential,
        resource: Optional[Resource] = None,
    ) -> None:
        """Check the Credential carries the material the App Check
        exchange needs.  No network I/O; messages name fields only."""
        self.ensure_supported(credential, resource)
        missing = [
            field
            for field in REQUIRED_FIREBASE_FIELDS
            if not str(credential.payload.get(field) or "")
        ]
        if missing:
            raise InvalidCredentialError(
                f"api_key credential '{credential.id}' missing required "
                f"material: {', '.join(missing)}",
                provider=self.provider_id,
                resource_id=resource.id if resource is not None else None,
            )

    async def get_runtime_credentials(
        self,
        credential: Credential,
        resource: Optional[Resource] = None,
    ) -> RuntimeCredentials:
        """Return the auth headers for one firebasevertexai call.

        Serves the cached JWT while it is valid (300s pre-refresh window
        honoured by the wrapped implementation) and exchanges the
        debug_token when the JWT is absent or near expiry.
        """
        self.ensure_supported(credential, resource)
        if resource is None:
            raise CredentialUnavailableError(
                "firebase auth requires a resource context (project_id)",
                provider=self.provider_id,
            )
        material = self.material_from(credential, resource)
        jwt = await self._auth.get_jwt(
            material["project_id"],
            material["app_id"],
            material["api_key"],
            material["debug_token"],
        )
        return RuntimeCredentials(
            headers={
                "x-goog-api-client": GOOG_API_CLIENT,
                "x-goog-api-key": material["api_key"],
                "X-Firebase-Appid": material["app_id"],
                "X-Firebase-AppCheck": jwt,
            },
            metadata={"project_id": material["project_id"]},
            expires_at=self._auth.expires_at,
        )

    async def refresh(
        self,
        credential: Credential,
        resource: Optional[Resource] = None,
    ) -> RuntimeCredentials:
        """Force a debug_token → App Check JWT exchange and return the
        fresh runtime credentials."""
        self.ensure_supported(credential, resource)
        if resource is None:
            raise CredentialUnavailableError(
                "firebase auth requires a resource context (project_id)",
                provider=self.provider_id,
            )
        material = self.material_from(credential, resource)
        try:
            jwt = await self._auth.get_jwt(
                material["project_id"],
                material["app_id"],
                material["api_key"],
                material["debug_token"],
                force=True,
            )
        except FirebaseNetworkError:
            raise  # transport problem: keep retryable network semantics
        except FirebaseTimeoutError:
            raise  # transport problem: keep retryable timeout semantics
        except FirebaseAuthError as exc:
            # Contract form of the existing auth failure.  The message
            # names the failure mode only — no debug token, api key or
            # JWT (the original error remains chained as __cause__).
            raise CredentialRefreshFailure(
                f"firebase auth: App Check exchange failed "
                f"({type(exc).__name__})",
                provider=self.provider_id,
                resource_id=resource.id,
            ) from exc
        return RuntimeCredentials(
            headers={
                "x-goog-api-client": GOOG_API_CLIENT,
                "x-goog-api-key": material["api_key"],
                "X-Firebase-Appid": material["app_id"],
                "X-Firebase-AppCheck": jwt,
            },
            metadata={"project_id": material["project_id"]},
            expires_at=self._auth.expires_at,
        )

    async def invalidate(
        self,
        credential: Credential,
        resource: Optional[Resource] = None,
    ) -> None:
        """Drop the runtime JWT cache.

        This is NOT a credential delete: ``debug_token`` / ``api_key`` /
        ``app_id`` in the Credential payload are untouched and the next
        get_runtime_credentials() call exchanges afresh.
        """
        self._auth.invalidate()
