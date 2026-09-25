"""Legacy credential → CredentialRepository migration (TASK-AUTH-010).

One-time, idempotent bootstrap migration: when a Provider Resource still
carries its legacy credential fields (AUTH-002 compatibility path) and
references no Credential, the durable material is copied into the
``CredentialRepository`` under a stable, deterministic id, and the
Resource is repointed at ``credential_id``::

    Resource (legacy fields, credential_id=None)
        ↓  migrate_legacy_resource_credentials()
    Credential(id="legacy-<provider>-<resource_id>", type, durable payload)
        ↓
    Resource.credential_id = "legacy-<provider>-<resource_id>"

Frozen rules:

* **Stable id** — ``legacy-<provider>-<resource_id>`` is a pure function
  of (provider, resource id), so repeated startups derive the same id:
  existing credentials are reused, never overwritten, and no second
  credential is ever created (idempotency does not depend on tracking
  state).
* **Provider durable mapping** — the field sets below mirror what the
  AUTH-004/005/006 adapters consume as durable material.  Runtime state
  is never migrated: gemini_cli's ``Resource.access_token`` is runtime
  cache input only and is excluded; Firebase's App Check JWT lives in
  runtime auth instances; antigravity's refreshed tokens and rotated
  refresh tokens stay in ``AntigravityAuth``.  The one deliberate
  exception is antigravity's legacy ``Resource.access_token``, which
  AUTH-006 established as a compatibility *durable* seed (static-token
  resources keep working through the credential path only if it is
  carried over).
* **Legacy fields stay** — they remain on the Resource as the
  compatibility path; migration only adds ``credential_id``.
* **Failure** — any repository error (e.g. PostgreSQL unavailable during
  a durable migration) propagates to the caller; startup must fail
  rather than silently continue with unresolved credentials.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping

from core.credential import (
    Credential,
    CredentialRepository,
    CredentialType,
    DuplicateCredentialError,
)
from core.resource import Resource

# Durable material per provider (AUTH-004/005/006 adapter contracts).
# Providers not listed here carry no legacy credential material
# (fake, anonymous_vertex).
LEGACY_DURABLE_FIELDS: Mapping[str, tuple] = {
    "gemini_cli": ("refresh_token", "client_id", "client_secret"),
    "firebase": ("api_key", "app_id", "debug_token"),
    # antigravity: access_token is the AUTH-006 compatibility durable
    # seed (static-token resources); refreshed/rotated tokens stay runtime.
    "antigravity": ("refresh_token", "client_id", "client_secret", "access_token"),
}

PROVIDER_CREDENTIAL_TYPE: Mapping[str, CredentialType] = {
    "gemini_cli": CredentialType.OAUTH,
    "firebase": CredentialType.API_KEY,
    "antigravity": CredentialType.OAUTH,
}


def legacy_credential_id(provider: str, resource_id: str) -> str:
    """Deterministic credential id for a legacy resource binding."""
    return f"legacy-{provider}-{resource_id}"


def migrate_legacy_resource_credentials(
    repository: CredentialRepository,
    resources: Iterable[Resource],
    *,
    field_map: Mapping[str, tuple] = LEGACY_DURABLE_FIELDS,
) -> int:
    """Migrate legacy resource credential fields into the repository.

    Returns the number of credentials newly created.  Re-runs are
    idempotent: already-migrated resources are recognized by their
    stable credential id and reused without overwriting.

    Repository failures propagate (caller decides startup semantics).
    """
    migrated = 0
    for resource in resources:
        provider = resource.provider
        if provider not in field_map:
            continue  # no legacy credential material for this provider
        if resource.credential_id:
            continue  # already bound: never re-migrate

        payload: Dict[str, Any] = {}
        for field in field_map[provider]:
            value = getattr(resource, field, None)
            if value:
                payload[field] = value
        if not payload:
            continue  # nothing durable to migrate (e.g. anonymous resource)

        credential_id = legacy_credential_id(provider, resource.id)
        if repository.get(credential_id) is None:
            try:
                repository.add(
                    Credential(
                        id=credential_id,
                        type=PROVIDER_CREDENTIAL_TYPE[provider],
                        payload=payload,
                    )
                )
                migrated += 1
            except DuplicateCredentialError:
                pass  # concurrent migration won the race: reuse, never overwrite
        resource.credential_id = credential_id
    return migrated
