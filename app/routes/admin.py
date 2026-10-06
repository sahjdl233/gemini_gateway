"""Authenticated management API: Antigravity Resources + Credentials."""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.management import ResourceManagementError, ResourceManager
from core.credential import (
    Credential,
    CredentialRepository,
    CredentialType,
    DuplicateCredentialError,
    UnknownCredentialError,
)
from core.credential_encryption import (
    ENCRYPTION_KEY_ID_ENV_VAR,
    ENCRYPTION_KEYS_ENV_VAR,
    CredentialEncryptionConfigError,
    CredentialEncryptor,
)
from core.credential_postgres import PostgreSQLCredentialRepository


router = APIRouter(prefix="/admin", tags=["admin"])


def _manager(request: Request) -> ResourceManager:
    return request.app.state.resource_manager


def _require_admin(request: Request) -> None:
    expected = os.environ.get("ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="admin API is not configured",
        )
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    supplied = authorization[7:]
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _json_object(request: Request) -> Dict[str, Any]:
    """Read an object without framework validation echoing credential input."""
    try:
        value = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return value


def _not_found(exc: ResourceManagementError) -> HTTPException:
    if str(exc) == "resource not found":
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


# WEBUI-002 §5/§7: the Admin page is a compiled Vue 3 + Vite SPA served from
# ``webui/dist``.  The Python module no longer embeds any HTML/JS: it only
# resolves the built entry, serves the hashed assets, and applies an SPA
# fallback for client-side routes.  The Admin *API* contract below is
# unchanged from WEBUI-001.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WEBUI_DIST = _REPO_ROOT / "webui" / "dist"
_WEBUI_ASSETS = _WEBUI_DIST / "assets"
_WEBUI_INDEX = _WEBUI_DIST / "index.html"

_MISSING_WEBUI_HINT = (
    "Admin WebUI bundle is not built. Run 'npm install && npm run build' "
    f"in {_REPO_ROOT / 'webui'} and restart the gateway."
)


def _webui_index_response() -> FileResponse:
    """Return the compiled SPA entry (WEBUI-002 acceptance)."""
    if not _WEBUI_INDEX.is_file():
        raise HTTPException(status_code=503, detail=_MISSING_WEBUI_HINT)
    return FileResponse(_WEBUI_INDEX, media_type="text/html")


@router.get("/", include_in_schema=False)
async def admin_page() -> FileResponse:
    """Serve the compiled SPA entry at ``/admin/`` (WEBUI-002 §5)."""
    return _webui_index_response()


@router.get("/resources")
async def list_resources(request: Request):
    _require_admin(request)
    # CONTROL-002: definition-sourced listing; optional provider filter
    # via query param (a {provider_id} path segment would collide with
    # the deprecated GET /resources/{resource_id} compat route).
    provider = request.query_params.get("provider")
    return await _manager(request).list_resources(provider)


# -- DB-RESOURCE-014: provider-scoped resource routes (canonical) -----------------


@router.get("/resources/{provider_id}/{resource_id}")
async def get_resource_scoped(
    provider_id: str, resource_id: str, request: Request
):
    _require_admin(request)
    try:
        return await _manager(request).get_definition_view(
            provider_id, resource_id
        )
    except ResourceManagementError as exc:
        raise _not_found(exc) from exc


@router.patch("/resources/{provider_id}/{resource_id}")
async def update_resource_scoped(
    provider_id: str, resource_id: str, request: Request
):
    _require_admin(request)
    payload = await _json_object(request)
    try:
        resource = await _manager(request).update_resource(
            provider_id, resource_id, payload
        )
        return _manager(request).serialize(resource)
    except ResourceManagementError as exc:
        raise _not_found(exc) from exc


@router.post("/resources/{provider_id}/{resource_id}/enable")
async def enable_resource_scoped(
    provider_id: str, resource_id: str, request: Request
):
    _require_admin(request)
    try:
        resource = await _manager(request).set_enabled(
            provider_id, resource_id, True
        )
        return _manager(request).serialize(resource)
    except ResourceManagementError as exc:
        raise _not_found(exc) from exc


@router.post("/resources/{provider_id}/{resource_id}/disable")
async def disable_resource_scoped(
    provider_id: str, resource_id: str, request: Request
):
    _require_admin(request)
    try:
        resource = await _manager(request).set_enabled(
            provider_id, resource_id, False
        )
        return _manager(request).serialize(resource)
    except ResourceManagementError as exc:
        raise _not_found(exc) from exc


@router.delete(
    "/resources/{provider_id}/{resource_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_resource_scoped(
    provider_id: str, resource_id: str, request: Request
):
    _require_admin(request)
    try:
        await _manager(request).delete_resource(provider_id, resource_id)
    except ResourceManagementError as exc:
        raise _not_found(exc) from exc


# -- DEPRECATED compatibility routes (antigravity default, WEBUI-002 era) ---------
#
# These resolve the legacy implicit provider and must not be used by new
# code; they exist so existing Admin WebUI builds and scripts keep working.


@router.get("/resources/{resource_id}")
async def get_resource(resource_id: str, request: Request):
    """Deprecated: use ``/resources/{provider_id}/{resource_id}``."""
    _require_admin(request)
    try:
        return await _manager(request).get_definition_view(
            "antigravity", resource_id
        )
    except ResourceManagementError as exc:
        raise _not_found(exc) from exc


@router.post("/resources", status_code=status.HTTP_201_CREATED)
async def create_resource(request: Request):
    """Create a resource.  ``provider`` in the body is the canonical form;
    when absent it resolves to the deprecated antigravity default."""
    _require_admin(request)
    payload = await _json_object(request)
    provider_id = payload.get("provider", "antigravity")
    try:
        resource = await _manager(request).create_resource(
            payload, provider_id=provider_id
        )
        return _manager(request).serialize(resource)
    except ResourceManagementError as exc:
        raise _not_found(exc) from exc


@router.patch("/resources/{resource_id}")
async def update_resource(resource_id: str, request: Request):
    """Deprecated: use ``/resources/{provider_id}/{resource_id}``."""
    _require_admin(request)
    payload = await _json_object(request)
    try:
        resource = await _manager(request).update_resource(
            "antigravity", resource_id, payload
        )
        return _manager(request).serialize(resource)
    except ResourceManagementError as exc:
        raise _not_found(exc) from exc


@router.post("/resources/{resource_id}/enable")
async def enable_resource(resource_id: str, request: Request):
    """Deprecated: use the provider-scoped route."""
    _require_admin(request)
    try:
        resource = await _manager(request).set_enabled(
            "antigravity", resource_id, True
        )
        return _manager(request).serialize(resource)
    except ResourceManagementError as exc:
        raise _not_found(exc) from exc


@router.post("/resources/{resource_id}/disable")
async def disable_resource(resource_id: str, request: Request):
    """Deprecated: use the provider-scoped route."""
    _require_admin(request)
    try:
        resource = await _manager(request).set_enabled(
            "antigravity", resource_id, False
        )
        return _manager(request).serialize(resource)
    except ResourceManagementError as exc:
        raise _not_found(exc) from exc


@router.delete("/resources/{resource_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_resource(resource_id: str, request: Request):
    """Deprecated: use ``/resources/{provider_id}/{resource_id}``."""
    _require_admin(request)
    try:
        await _manager(request).delete_resource("antigravity", resource_id)
    except ResourceManagementError as exc:
        raise _not_found(exc) from exc


# -- Credential management (AUTH-011) --------------------------------------------
#
# All operations go through the application-wide CredentialRepository
# (``app.state.credential_store``) — never through PostgreSQL directly.
# Responses carry the AUTH-002 redacted view only: secret-shaped payload
# values are masked, plaintext secrets never leave the process.  In
# postgres mode the repository persists encrypted envelopes; in memory
# mode behaviour is unchanged (AUTH-002 in-memory store).


def _credential_repository(request: Request) -> CredentialRepository:
    return request.app.state.credential_store


def _credential_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, UnknownCredentialError):
        return HTTPException(status_code=404, detail="credential not found")
    if isinstance(exc, DuplicateCredentialError):
        return HTTPException(status_code=409, detail="credential already exists")
    return HTTPException(status_code=400, detail=str(exc))


def _credential_view(request: Request, credential: Credential) -> Dict[str, Any]:
    """Redacted credential view plus the AUTH-013 reference flag.

    WEBUI-001: the WebUI needs to know whether a Credential is still bound to
    a live Resource (it blocks DELETE with 409), so the flag is part of every
    credential response.  The payload itself stays redacted: no plaintext
    secret ever reaches the response, the HTML, or the page's JS state.
    """
    view = credential.redacted_dict()
    view["referenced"] = _credential_is_referenced(
        request, credential.id
    )
    return view


@router.get("/credentials")
async def list_credentials(request: Request):
    _require_admin(request)
    repository = _credential_repository(request)
    return [
        _credential_view(request, credential) for credential in repository.list()
    ]


@router.get("/credentials/{credential_id}")
async def get_credential(credential_id: str, request: Request):
    _require_admin(request)
    repository = _credential_repository(request)
    try:
        credential = repository.require(credential_id)
    except UnknownCredentialError as exc:
        raise _credential_http_error(exc) from exc
    return _credential_view(request, credential)


@router.post("/credentials", status_code=status.HTTP_201_CREATED)
async def create_credential(request: Request):
    _require_admin(request)
    body = await _json_object(request)
    credential_id = body.get("id")
    if not isinstance(credential_id, str) or not credential_id.strip():
        raise HTTPException(status_code=400, detail="credential id is required")
    raw_type = body.get("type", "none")
    try:
        credential_type = CredentialType(str(raw_type))
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"unknown credential type: {raw_type!r}"
        ) from exc
    payload = body.get("payload", None)
    if not isinstance(payload, dict):
        # payload must be a JSON object: null / array / string are all
        # rejected (AUTH-011 fix; no `or {}` fallback).
        raise HTTPException(
            status_code=400, detail="body must contain a 'payload' object"
        )
    try:
        credential = Credential(
            id=credential_id, type=credential_type, payload=payload
        )
        repository = _credential_repository(request)
        repository.add(credential)
    except (DuplicateCredentialError, UnknownCredentialError) as exc:
        raise _credential_http_error(exc) from exc
    return _credential_view(request, credential)


@router.patch("/credentials/{credential_id}")
async def update_credential_payload(credential_id: str, request: Request):
    _require_admin(request)
    body = await _json_object(request)
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400, detail="body must contain a 'payload' object"
        )
    manager = _manager(request)
    # AUTH-016 B: serialized against resource mutations — a concurrent
    # resource write must not bind this credential while its payload is
    # being replaced (or removed).
    async with manager.mutation_lock():
        try:
            credential = _credential_repository(request).update_payload(
                credential_id, payload
            )
        except UnknownCredentialError as exc:
            raise _credential_http_error(exc) from exc
        # AUTH-016 A: the per-resource adapters hold OAuth runtime state
        # (notably the rotated refresh token, which wins over store
        # material on the next refresh).  An operator's payload
        # replacement must take effect on the NEXT request, so every
        # resource bound to this credential gets its adapter dropped;
        # the fresh adapter re-resolves material from the new payload.
        # Rotation (AUTH-014) is unaffected: it persists through the
        # listener inside the refresh flow, never through this route.
        await _invalidate_credential_adapters(request, credential_id)
    return _credential_view(request, credential)


def _credential_is_referenced(request: Request, credential_id: str) -> bool:
    """True when any runtime Resource references this credential.

    Reference check is based on the live scheduler pools (AUTH-013):
    deleting a bound credential would leave the resource with a dangling
    reference, which the adapters now fail closed on.
    """
    scheduler = getattr(request.app.state, "scheduler", None)
    for pool in getattr(scheduler, "pools", {}).values():
        for resource in pool.resources:
            if resource.credential_id == credential_id:
                return True
    return False


async def _invalidate_credential_adapters(
    request: Request, credential_id: str
) -> int:
    """Drop the per-resource auth adapter of every resource bound to a
    credential whose payload was just mutated (AUTH-016 A).

    Uses the same optional ``provider.invalidate_resource`` capability as
    the resource-mutation path (CONTROL-006-FIX-1).  Returns the number
    of invalidated adapters."""
    scheduler = getattr(request.app.state, "scheduler", None)
    invalidated = 0
    for provider_id, pool in getattr(scheduler, "pools", {}).items():
        provider = getattr(scheduler, "providers", {}).get(provider_id)
        invalidate = getattr(provider, "invalidate_resource", None)
        if invalidate is None:
            continue
        from core.provider import await_if_needed

        for resource in pool.resources:
            if resource.credential_id == credential_id:
                await await_if_needed(invalidate(resource.id))
                invalidated += 1
    return invalidated


@router.delete(
    "/credentials/{credential_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_credential(credential_id: str, request: Request):
    _require_admin(request)
    manager = _manager(request)
    # AUTH-016 B: the reference check and the removal happen under the
    # same lock that serializes resource mutations — a concurrent
    # resource write can neither bind this credential between check and
    # remove (binding a deleted credential), nor have its own existence
    # validation pass against a credential this route is about to
    # remove.
    async with manager.mutation_lock():
        # Delete protection (AUTH-013): a credential still referenced by any
        # Resource must not be removed (identical for memory and postgres).
        if _credential_is_referenced(request, credential_id):
            raise HTTPException(
                status_code=409,
                detail="credential is still referenced by one or more resources",
            )
        # Idempotent per the repository contract: unknown ids are a no-op.
        _credential_repository(request).remove(credential_id)


@router.post("/credentials/rotate-key")
async def rotate_credential_key(request: Request):
    """Bulk re-encryption of durable credential payloads (AUTH-012).

    Postgres mode only: memory mode has nothing durable to rotate (400).
    Requires the keyring configuration (GEMINI_GATEWAY_ENCRYPTION_KEYS +
    GEMINI_GATEWAY_ENCRYPTION_KEY_ID) to be present; a missing or invalid
    configuration is a 400, never a silent no-op.  Returns rotation
    counts only — no credential payload material.
    """
    _require_admin(request)
    repository = _credential_repository(request)
    if not isinstance(repository, PostgreSQLCredentialRepository):
        raise HTTPException(
            status_code=400,
            detail="key rotation requires a durable credential repository",
        )
    if (
        os.environ.get(ENCRYPTION_KEYS_ENV_VAR) is None
        or os.environ.get(ENCRYPTION_KEY_ID_ENV_VAR) is None
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "key rotation requires GEMINI_GATEWAY_ENCRYPTION_KEYS and "
                "GEMINI_GATEWAY_ENCRYPTION_KEY_ID to be configured"
            ),
        )
    try:
        new_encryptor = CredentialEncryptor.from_environment()
    except CredentialEncryptionConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return repository.rotate_key(new_encryptor)


@router.get("/{spa_path:path}", include_in_schema=False)
async def admin_spa_fallback(spa_path: str) -> FileResponse:
    """SPA fallback for client-side routes (WEBUI-002 §5).

    Declared last so every API route above wins the match; a deep link such as
    ``/admin/credentials`` from a browser refresh resolves to the same entry
    document the SPA router then handles in the browser.
    """
    del spa_path  # the SPA owns its own routing
    return _webui_index_response()


def mount_admin_assets(app: Any) -> bool:
    """Serve ``/admin/assets/...`` from the Vite build output (WEBUI-002 §5).

    ``APIRouter.mount`` would register the mount at the application root
    (ignoring ``prefix``), which would publish the bundle at ``/assets/...``
    instead of the ``/admin/assets/...`` URLs the built HTML references.  The
    mount therefore has to be applied to the app by :func:`create_app`.

    Returns ``True`` when the bundle is present.  When ``webui/dist`` has not
    been built the route is simply not registered and ``/admin/`` answers 503
    with build instructions instead of crashing the whole gateway.
    """
    if not _WEBUI_ASSETS.is_dir():
        return False
    app.mount(
        "/admin/assets",
        StaticFiles(directory=str(_WEBUI_ASSETS)),
        name="admin-assets",
    )
    return True
