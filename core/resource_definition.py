"""Persisted Resource Definition DTOs (DB-RESOURCE-001-1).

This module is the persistence boundary between a stored Resource
Definition and the runtime core.resource.Resource hierarchy.  It is
deliberately separate from the runtime models:

* runtime Resource (core/resource.py:60-84) mixes configuration with live
  scheduling state (health, cooldown_until, in_flight, total_requests,
  total_failures, consecutive_failures) and does not forbid extra fields;
* a ResourceDefinition carries only what may be written to a durable store:
  identity, enabled flag, a credential reference, and the provider-specific
  non-secret configuration.

Design baseline: docs/DB-RESOURCE-DESIGN-001.md (rev. 2).

Rules enforced here (ADR sections 2.1 and 3):

* provider is the discriminant; unknown providers are a hard error and are
  never skipped, coerced, or downgraded to a plain dict.
* Every model sets extra="forbid", so unknown fields, runtime-only fields,
  and secret fields all fail loudly instead of being dropped.
* No secret material is representable.  credential_id is a reference; the
  DTO layer never resolves it and never imports a CredentialRepository.
* proxy may only be persisted when it carries no authentication material.

Runtime reload, Repository/PostgreSQL persistence, startup bootstrap, YAML
import/export, and Admin API integration are out of scope for this module.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Mapping, Optional, Type, Union
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, StrictBool, field_validator
from pydantic import ValidationError
from pydantic_core import PydanticCustomError


class ResourceDefinitionError(ValueError):
    """Base class for every Resource Definition validation failure."""


class UnknownProviderError(ResourceDefinitionError):
    """Raised when provider does not name a known Resource type."""


class CredentialBearingProxyError(ResourceDefinitionError):
    """Raised when a proxy value embeds authentication material."""


#: Pydantic error ``type`` used so a credential-bearing proxy survives
#: validation as a typed error instead of a generic string blob.
_PROXY_ERROR_TYPE = "credential_bearing_proxy"
_EMPTY_PROXY_ERROR_TYPE = "empty_proxy"


def _proxy_error(message: str) -> PydanticCustomError:
    return PydanticCustomError(_PROXY_ERROR_TYPE, message)  # type: ignore[arg-type]


def _empty_proxy_error(message: str) -> PydanticCustomError:
    return PydanticCustomError(_EMPTY_PROXY_ERROR_TYPE, message)  # type: ignore[arg-type]


# Query-string keys that indicate a proxy URL is carrying a secret.  A
# credential-free proxy endpoint is the only shape allowed to persist.
_CREDENTIAL_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "credential",
        "key",
        "passwd",
        "password",
        "pwd",
        "secret",
        "session",
        "sig",
        "signature",
        "token",
        "user",
        "username",
    }
)


def _validate_credential_free_proxy(value: Optional[str]) -> Optional[str]:
    """Reject any proxy string that embeds credentials.

    Only a bare endpoint (scheme://host:port) is persistable.  Rejected:

    * userinfo, e.g. http://user:password@example.com;
    * any query parameter whose name suggests a secret, e.g.
      socks5://host:1080?token=abc.

    This is the minimal validation the persistence boundary needs; it is not a
    general proxy security framework.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise _proxy_error("proxy must be a string or null")

    candidate = value.strip()
    if not candidate:
        raise _empty_proxy_error("proxy must not be empty")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:  # pragma: no cover - urlsplit rarely raises
        raise ResourceDefinitionError("proxy is not a valid URL") from exc

    if parts.username or parts.password or "@" in parts.netloc:
        raise _proxy_error(
            "proxy must not contain userinfo; use a credential reference"
        )

    for key, _ in parse_qsl(parts.query, keep_blank_values=True):
        if key.strip().lower() in _CREDENTIAL_QUERY_KEYS:
            raise _proxy_error(
                f"proxy must not contain credential query parameter: {key}"
            )

    return candidate


def _definition_body(dto: "ResourceDefinitionBase") -> Dict[str, Any]:
    """Dump every field except the four common ones."""

    common = {"provider", "id", "enabled", "credential_id"}
    return dto.model_dump(exclude=common, exclude_none=True)


class ResourceDefinitionBase(BaseModel):
    """Common, provider-independent persisted fields.

    Identity is the composite (provider, id) pair defined by
    core.resource.ResourceKey; id is deliberately not globally unique, and
    this DTO enforces no uniqueness of its own.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    id: str
    # StrictBool mirrors the project's existing strictness for this field
    # (app/management.py:206-207); a plain bool would silently accept
    # "yes" / "1" / "on".
    enabled: StrictBool = True
    # Reference only.  Existence and integrity are enforced by later layers;
    # this module never resolves it and never rewrites it to None.
    credential_id: Optional[str] = None

    def to_definition_json(self) -> Dict[str, Any]:
        """Return only the provider-specific, non-secret definition body.

        This is the payload a future repository would store in its JSONB
        definition column.  Common fields (identity, enabled, credential_id)
        are stored in dedicated columns and are therefore excluded here.
        """

        return _definition_body(self)

    def to_runtime_definition(self) -> Dict[str, Any]:
        """Return the raw definition consumed by ResourceFactory.

        The result is exactly to_definition_json() plus the common fields,
        which is what ProviderRegistry.create_resources (and every provider
        factory) forwards to Resource.model_validate.  It carries no runtime
        state and no secret material.
        """

        payload = self.to_definition_json()
        payload["provider"] = self.provider
        payload["id"] = self.id
        payload["enabled"] = self.enabled
        payload["credential_id"] = self.credential_id
        return payload


class AntigravityResourceDefinition(ResourceDefinitionBase):
    """ADR section 3 allowlist: project_id and ide_type only.

    access_token, refresh_token, client_id, client_secret and token_expiry
    are rejected.  token_expiry is auth/runtime lifecycle state and never
    enters a persisted definition.
    """

    provider: Literal["antigravity"] = "antigravity"
    project_id: Optional[str] = None
    ide_type: str = "ANTIGRAVITY"


class GeminiCliResourceDefinition(ResourceDefinitionBase):
    """ADR section 3 allowlist, including a credential-free proxy only."""

    provider: Literal["gemini_cli"] = "gemini_cli"
    project_id: Optional[str] = None
    tier: str = "unknown"
    pinned_model: Optional[str] = None
    proxy: Optional[str] = None
    ide_type: str = "GCLI"
    platform: str = "PLATFORM_UNSPECIFIED"
    plugin_type: str = "GEMINI"
    preview: StrictBool = True

    @field_validator("proxy")
    @classmethod
    def _reject_credential_bearing_proxy(cls, value: Optional[str]) -> Optional[str]:
        return _validate_credential_free_proxy(value)


class FirebaseResourceDefinition(ResourceDefinitionBase):
    """ADR section 3 allowlist.

    api_key, app_id and debug_token are Credential material
    (core/credential_migration.py:56) and are rejected here; they are
    reachable only through credential_id.
    """

    provider: Literal["firebase"] = "firebase"
    project_id: Optional[str] = None
    pinned_model: Optional[str] = None
    proxy: Optional[str] = None

    @field_validator("proxy")
    @classmethod
    def _reject_credential_bearing_proxy(cls, value: Optional[str]) -> Optional[str]:
        return _validate_credential_free_proxy(value)


class AnonymousVertexResourceDefinition(ResourceDefinitionBase):
    """ADR section 3 allowlist.

    v1 does not implement a Credential owner for Anonymous Vertex proxy
    credentials, so proxy_username and proxy_password are rejected and a
    credential-bearing proxy configuration cannot be persisted.
    """

    provider: Literal["anonymous_vertex"] = "anonymous_vertex"
    proxy_scheme: str = "direct"
    proxy_host: Optional[str] = None
    proxy_port: Optional[int] = None
    pinned_model: Optional[str] = None


class FakeResourceDefinition(ResourceDefinitionBase):
    """ADR section 3 allowlist.  The fake provider carries no secret field."""

    provider: Literal["fake"] = "fake"
    scenario: str = "success"
    retry_after: Optional[float] = None
    reply_text: str = "Hello from FakeProvider!"
    model_ids: Optional[List[str]] = None


ResourceDefinition = Union[
    AntigravityResourceDefinition,
    GeminiCliResourceDefinition,
    FirebaseResourceDefinition,
    AnonymousVertexResourceDefinition,
    FakeResourceDefinition,
]


# Discriminant -> strict DTO.  This is the single dispatch table used by
# parse_resource_definition; adding a provider means adding an entry here.
PROVIDER_DEFINITION_TYPES: Dict[str, Type[ResourceDefinitionBase]] = {
    "antigravity": AntigravityResourceDefinition,
    "gemini_cli": GeminiCliResourceDefinition,
    "firebase": FirebaseResourceDefinition,
    "anonymous_vertex": AnonymousVertexResourceDefinition,
    "fake": FakeResourceDefinition,
}


def parse_resource_definition(payload: Mapping[str, Any]) -> ResourceDefinition:
    """Strictly validate payload into the DTO selected by provider.

    Raises ResourceDefinitionError when the payload is not a mapping, has no
    usable provider, names an unknown provider, or fails field validation
    (unknown field, runtime-only field, secret field, type error).
    """

    if not isinstance(payload, Mapping):
        raise ResourceDefinitionError(
            f"resource definition must be a mapping, got {type(payload).__name__}"
        )

    provider = payload.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise ResourceDefinitionError("resource definition requires a provider")

    dto_type = PROVIDER_DEFINITION_TYPES.get(provider)
    if dto_type is None:
        raise UnknownProviderError(f"unknown resource provider: {provider!r}")

    try:
        return dto_type.model_validate(dict(payload))
    except ValidationError as exc:
        raise _restore_typed_errors(exc) from exc


def _restore_typed_errors(exc: ValidationError) -> Exception:
    """Re-raise a proxy rejection as CredentialBearingProxyError.

    Pydantic wraps exceptions raised inside a validator.  Callers of the DTO
    layer need a stable, typed signal that the failure was specifically a
    credential-bearing proxy rather than a generic validation error, so the
    custom error type is translated back here.
    """

    for error in exc.errors():
        if error.get("type") == _PROXY_ERROR_TYPE:
            return CredentialBearingProxyError(str(error.get("msg", "")))
        if error.get("type") == _EMPTY_PROXY_ERROR_TYPE:
            return ResourceDefinitionError(str(error.get("msg", "")))
    return exc


def resource_definition_from_row(
    *,
    provider: str,
    resource_id: str,
    enabled: bool,
    credential_id: Optional[str],
    definition: Mapping[str, Any],
) -> ResourceDefinition:
    """Rebuild a strict DTO from repository columns plus a JSONB body.

    provider / resource_id / enabled / credential_id come from the identity
    columns; definition is the provider-specific JSONB payload produced by
    to_definition_json().  A provider key inside definition is rejected
    rather than trusted, so the columns stay authoritative.
    """

    if not isinstance(definition, Mapping):
        raise ResourceDefinitionError("stored definition must be a mapping")
    if "provider" in definition:
        raise ResourceDefinitionError(
            "stored definition must not repeat the provider identity column"
        )

    payload: Dict[str, Any] = dict(definition)
    payload["provider"] = provider
    payload["id"] = resource_id
    payload["enabled"] = enabled
    payload["credential_id"] = credential_id
    return parse_resource_definition(payload)


__all__ = [
    "AnonymousVertexResourceDefinition",
    "AntigravityResourceDefinition",
    "CredentialBearingProxyError",
    "FakeResourceDefinition",
    "FirebaseResourceDefinition",
    "GeminiCliResourceDefinition",
    "PROVIDER_DEFINITION_TYPES",
    "ResourceDefinition",
    "ResourceDefinitionBase",
    "ResourceDefinitionError",
    "UnknownProviderError",
    "parse_resource_definition",
    "resource_definition_from_row",
]
