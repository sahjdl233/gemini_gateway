"""ProviderAuthAdapter contract (TASK-AUTH-003).

The formal boundary between Credentials and provider-specific
authentication protocols::

    Resource ──credential_id──→ Credential ──→ ProviderAuthAdapter ──→ Provider

Responsibilities frozen by AUTH-003:

* **Credential** = durable authentication material (refresh tokens,
  client secrets, API keys).  It knows no endpoints and no protocols.
* **ProviderAuthAdapter** = authentication protocol + runtime lifecycle:
  validate material, produce per-request runtime credentials, refresh,
  invalidate.  It may know oauth2.googleapis.com, Firebase App Check,
  SAPISIDHASH, ... — Credential may not.
* **RuntimeCredentials** = short-lived runtime output (auth headers +
  metadata + expiry).  It is NOT durable and must never be written back
  into a Credential.  It is NOT an HTTP request object: no url, method,
  body, model, request or response — transport stays with the
  ExecutionBackend.

Error semantics:

* Adapter failures subclass :class:`core.errors.AuthenticationError`, so
  the Scheduler sees them as plain 401-class provider errors (non
  retryable) and never learns about OAuth, refresh tokens or JWTs.
* A 429 is a rate limit, not a credential failure.  Adapters must not
  map upstream HTTP errors onto Credential errors unless the provider
  protocol clearly makes them authentication failures (previous
  antigravity/vertex behaviour where 429 surfaced as 401 must NOT be
  encoded into this contract).

Lifecycle scope:

* Adapters are owned by Providers.  The contract does not mandate a
  runtime cache scope: one Credential shared by N Resources does not
  require a shared runtime token cache.  CURRENT provider behaviour
  (GeminiCliAuth / FirebaseAuth) is resource-scoped (one auth instance
  per resource); whether a credential-scoped cache is introduced is an
  AUTH-004/005 implementation decision, not part of this contract.

Out of scope for this module (and forbidden in it): persistence
(CredentialStore stays an in-memory registry), encryption, browser
automation, Supabase, and any concrete Google protocol implementation
(those arrive as GeminiCliAuthAdapter / FirebaseAuthAdapter /
AntigravityAuthAdapter in AUTH-004/005/006).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Dict, Optional

from pydantic import BaseModel, Field

from .credential import Credential, CredentialType, redact_payload
from .errors import AuthenticationError
from .resource import Resource


# -- adapter error contract ----------------------------------------------------
#
# Mapping against the existing core.errors hierarchy (AUTH-003):
#   AuthenticationFailure  -> core.errors.AuthenticationError (existing, reused)
#   InvalidCredential      -> InvalidCredentialError (new)
#   CredentialRefreshFailure -> CredentialRefreshFailure (new)
#   CredentialUnavailable  -> CredentialUnavailableError (new)


class CredentialError(AuthenticationError):
    """Base for credential-lifecycle failures raised by auth adapters.

    Subclasses :class:`core.errors.AuthenticationError` deliberately: the
    Scheduler and error mapper treat these exactly like a 401 — never
    retryable, never credential-protocol-aware.
    """

    default_status = 401


class InvalidCredentialError(CredentialError):
    """Credential material is missing or invalid for this adapter's
    protocol (e.g. an oauth credential without refresh_token)."""

    default_status = 401


class CredentialRefreshFailure(CredentialError):
    """The provider-specific refresh protocol failed (upstream rejected
    the refresh, endpoint unreachable with an auth error, ...)."""

    default_status = 401


class CredentialUnavailableError(CredentialError):
    """No usable credential for this request: unknown credential_id,
    unsupported CredentialType for this adapter, or an explicit
    type=none credential where material is required."""

    default_status = 401


# -- runtime credential representation ------------------------------------------


class RuntimeCredentials(BaseModel):
    """Short-lived authentication material for exactly one upstream call.

    Deliberately minimal and transport-agnostic: auth ``headers`` the
    Provider merges into its request, provider-specific ``metadata``
    (e.g. a project id that must travel in the URL), and ``expires_at``
    (epoch seconds, ``None`` = no known expiry).  No url, method, body,
    model, request or response — the ExecutionBackend owns transport.

    ``repr``/``str`` never reveal secret values (header values are fully
    masked; secret-shaped metadata keys follow the Credential rules).
    """

    headers: Dict[str, str] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    expires_at: Optional[float] = None

    def redacted_dict(self) -> Dict[str, Any]:
        """View safe for logs / API responses."""
        return {
            "headers": {name: "***" for name in self.headers},
            "metadata": redact_payload(self.metadata),
            "expires_at": self.expires_at,
        }

    def __repr__(self) -> str:
        return (
            f"RuntimeCredentials("
            f"headers={{ {', '.join(repr(k) + ': ' + repr('***') for k in self.headers)} }}, "
            f"metadata={redact_payload(self.metadata)!r}, "
            f"expires_at={self.expires_at!r})"
        )

    def __str__(self) -> str:
        return repr(self)


# -- the contract ----------------------------------------------------------------


class ProviderAuthAdapter(ABC):
    """Authentication lifecycle contract every provider auth adapter implements.

    Concrete adapters are provider-specific by design
    (``GeminiCliAuthAdapter``, ``FirebaseAuthAdapter``,
    ``AntigravityAuthAdapter``, ...) — there is deliberately no universal
    Google auth adapter.  What they share is this interface and its
    lifecycle semantics, not their protocols.

    Instances are owned by the Provider; the Scheduler never sees an
    adapter, a Credential, or RuntimeCredentials.  ``resource`` is always
    per-call context (identity / provider configuration such as
    project_id) — an adapter must not store it as permanent state.
    """

    #: CredentialTypes this adapter's protocol can handle.  Adapters
    #: declare compatibility; :meth:`ensure_supported` enforces it.
    credential_types: ClassVar[frozenset[CredentialType]] = frozenset()

    @abstractmethod
    async def validate(
        self,
        credential: Credential,
        resource: Optional[Resource] = None,
    ) -> None:
        """Check the credential carries the material this protocol needs.

        Return normally when valid; raise :class:`InvalidCredentialError`
        (or :class:`CredentialUnavailableError` for unsupported types)
        otherwise.  No network I/O is required — validate is about
        material presence/shape, not upstream acceptance.
        """

    @abstractmethod
    async def get_runtime_credentials(
        self,
        credential: Credential,
        resource: Optional[Resource] = None,
    ) -> RuntimeCredentials:
        """Produce the runtime auth material for one upstream call.

        Typically serves from the adapter's runtime cache and refreshes
        when near expiry.  Raises :class:`CredentialUnavailableError`
        when no runtime material can be produced.
        """

    @abstractmethod
    async def refresh(
        self,
        credential: Credential,
        resource: Optional[Resource] = None,
    ) -> RuntimeCredentials:
        """Run the provider-specific refresh protocol and return fresh
        runtime credentials.

        Raises :class:`CredentialRefreshFailure` when the refresh fails.
        Implementations must cap retries themselves — an adapter never
        loops refresh indefinitely.
        """

    @abstractmethod
    async def invalidate(
        self,
        credential: Credential,
        resource: Optional[Resource] = None,
    ) -> None:
        """Drop the runtime credential cache (e.g. after a 401).

        The next :meth:`get_runtime_credentials` call must reacquire or
        force-refresh.  Must be safe to call repeatedly.
        """

    # -- concrete helpers ---------------------------------------------------

    def supports(self, credential: Credential) -> bool:
        """True when this adapter's protocol handles the credential type."""
        return credential.type in self.credential_types

    def ensure_supported(
        self,
        credential: Credential,
        resource: Optional[Resource] = None,
    ) -> None:
        """Raise :class:`CredentialUnavailableError` for unsupported types."""
        if not self.supports(credential):
            raise CredentialUnavailableError(
                f"credential '{credential.id}' has type "
                f"'{credential.type.value}', which this adapter "
                f"({type(self).__name__}) does not support",
                provider=getattr(self, "provider_id", "unknown"),
                resource_id=resource.id if resource is not None else None,
            )


def require_bound_credential(
    credential_id: str,
    resource: Resource,
    *,
    store: Optional[Any],
    expected_type: CredentialType,
    provider_id: str,
) -> Credential:
    """Strict reference resolution for ``Resource.credential_id`` (AUTH-013).

    Once a Resource references a Credential, that reference is binding:
    the credential must exist in ``store`` and carry ``expected_type``.
    Any violation raises :class:`CredentialUnavailableError` — adapters
    must NEVER silently fall back to legacy Resource fields while a
    ``credential_id`` is set (a deleted credential would otherwise
    resurrect the old secrets).  ``credential_id=None`` never reaches
    this helper: callers keep their legacy compatibility path for it.
    """
    if store is None:
        raise CredentialUnavailableError(
            f"resource '{resource.id}' references credential "
            f"'{credential_id}' but no credential repository is attached",
            provider=provider_id,
            resource_id=resource.id,
        )
    credential = store.get(credential_id)
    if credential is None:
        raise CredentialUnavailableError(
            f"credential '{credential_id}' referenced by resource "
            f"'{resource.id}' is not registered",
            provider=provider_id,
            resource_id=resource.id,
        )
    if credential.type is not expected_type:
        raise CredentialUnavailableError(
            f"credential '{credential_id}' has type "
            f"'{credential.type.value}', expected '{expected_type.value}'",
            provider=provider_id,
            resource_id=resource.id,
        )
    return credential
