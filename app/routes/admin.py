"""Authenticated management API: Antigravity Resources + Credentials."""

from __future__ import annotations

import hmac
import json
import os
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse

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


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def admin_page() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gemini Gateway Admin</title>
  <style>
    body{font:15px system-ui,sans-serif;max-width:1180px;margin:2rem auto;padding:0 1rem;color:#222}
    table{border-collapse:collapse;width:100%}th,td{padding:.55rem;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}
    input{width:100%;box-sizing:border-box;padding:.45rem;margin:.2rem 0}
    button{cursor:pointer;margin:.15rem;padding:.35rem .6rem}
    form{border:1px solid #ddd;padding:1rem;margin:1rem 0;max-width:720px}
    label{display:block;margin:.45rem 0}.muted{color:#666}.message{min-height:1.4em}
    .disabled{opacity:.65}dialog{border:1px solid #aaa;padding:1rem;max-width:460px}
    h2{margin-top:2.2rem;border-bottom:1px solid #ddd;padding-bottom:.3rem}
    h3{margin-top:1.4rem}
    textarea{width:100%;box-sizing:border-box;font-family:ui-monospace,monospace;padding:.45rem}
    code{font-family:ui-monospace,monospace;font-size:.9em;word-break:break-all}
  </style>
</head>
<body>
  <h1>Gemini Gateway</h1>
  <p class="muted">Dashboard &mdash; runtime Resources and long-lived Credentials</p>
  <p>
    <label>Admin token
      <input id="token" type="password" autocomplete="off" size="40">
    </label>
    <button id="load" type="button">Refresh</button>
  </p>
  <div id="message" class="message"></div>

  <h2>Resources</h2>
  <p class="muted">
    Runtime configuration and scheduling only. Long-lived credential material is
    owned by a Credential and reached through <code>credential_id</code>.
  </p>
  <table id="resource-table">
    <thead><tr>
      <th>ID</th><th>Provider</th><th>Credential</th><th>Health</th><th>Cooldown</th>
      <th>In flight</th><th>Requests</th><th>Failures</th><th>Actions</th>
    </tr></thead>
    <tbody id="resources"></tbody>
  </table>
  <h3 id="form-title">Add Resource</h3>
  <form id="resource-form">
    <input type="hidden" name="original_id">
    <label>Resource ID<input name="id" required></label>
    <label>Credential ID<input name="credential_id" autocomplete="off"></label>
    <label>Project ID<input name="project_id"></label>
    <label>IDE Type<input name="ide_type" value="ANTIGRAVITY"></label>
    <label><input type="checkbox" name="enabled" checked> Enabled</label>
    <button type="submit">Save</button>
    <button id="cancel-edit" type="button" hidden>Cancel edit</button>
  </form>

  <h2 id="credentials">Credentials</h2>
  <p class="muted">
    Managed through the CredentialRepository. Payloads are always redacted on
    read; a stored payload is never rendered back into this page.
  </p>
  <p><button id="new-credential" type="button">New Credential</button></p>
  <table id="credential-table">
    <thead><tr>
      <th>ID</th><th>Type</th><th>Payload (redacted)</th><th>Referenced</th>
      <th>Created</th><th>Updated</th><th>Actions</th>
    </tr></thead>
    <tbody id="credential-rows"></tbody>
  </table>

  <dialog id="credential-dialog">
    <form id="credential-form">
      <h3 id="credential-dialog-title">New Credential</h3>
      <label>Credential ID<input name="id" autocomplete="off" required></label>
      <label>Type
        <select name="type">
          <option value="none">none</option>
          <option value="oauth">oauth</option>
          <option value="api_key">api_key</option>
        </select>
      </label>
      <label>Payload JSON
        <textarea name="payload" rows="8" spellcheck="false" placeholder='{&quot;refresh_token&quot;: &quot;...&quot;}'></textarea>
      </label>
      <p class="muted" id="payload-hint"></p>
      <button type="submit">Save</button>
      <button type="button" id="credential-cancel">Cancel</button>
    </form>
  </dialog>
<script>
const token = document.getElementById('token');
const message = document.getElementById('message');
const form = document.getElementById('resource-form');
const tbody = document.getElementById('resources');
const credTbody = document.getElementById('credential-rows');
const credDialog = document.getElementById('credential-dialog');
const credForm = document.getElementById('credential-form');
let rows = [];
let credentials = [];
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
  }[c]));
}
async function api(path, options = {}) {
  const headers = {'Authorization': 'Bearer ' + token.value};
  if (options.body) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {...options, headers});
  const text = await response.text();
  if (!response.ok) {
    let detail = text;
    try { detail = JSON.parse(text).detail || text; } catch (_) {}
    throw new Error(detail);
  }
  return text ? JSON.parse(text) : null;
}
function renderResources() {
  tbody.innerHTML = rows.map(r => `<tr class="${r.enabled ? '' : 'disabled'}">
    <td>${esc(r.id)}</td><td>${esc(r.provider)}</td>
    <td>${esc(r.credential_id || '')}</td>
    <td>${esc(r.enabled ? 'enabled' : 'disabled')}</td>
    <td>${esc(r.health)}</td><td>${esc(r.cooldown_until || 'none')}</td>
    <td>${esc(r.in_flight)}</td><td>${esc(r.total_requests)}</td><td>${esc(r.total_failures)}</td>
    <td>
      <button data-action="enabled" data-id="${esc(r.id)}" data-value="${!r.enabled}">${r.enabled ? 'Disable' : 'Enable'}</button>
      <button data-action="edit" data-id="${esc(r.id)}">Edit</button>
      <button data-action="delete" data-id="${esc(r.id)}">Delete</button>
    </td></tr>`).join('');
}
function renderCredentials() {
  credTbody.innerHTML = credentials.map(c => `<tr>
    <td>${esc(c.id)}</td><td>${esc(c.type)}</td>
    <td><code>${esc(JSON.stringify(c.payload || {}))}</code></td>
    <td>${c.referenced ? 'yes' : 'no'}</td>
    <td>${esc(c.created_at || '')}</td><td>${esc(c.updated_at || '')}</td>
    <td>
      <button data-action="cred-edit" data-id="${esc(c.id)}">Edit</button>
      <button data-action="cred-delete" data-id="${esc(c.id)}">Delete</button>
    </td></tr>`).join('');
}
async function loadResources() {
  try { rows = await api('/admin/resources'); renderResources(); message.textContent = ''; }
  catch (error) { message.textContent = error.message; }
}
async function loadCredentials() {
  try { credentials = await api('/admin/credentials'); renderCredentials(); }
  catch (error) { message.textContent = error.message; }
}
function resetForm() {
  form.reset(); form.elements.original_id.value = '';
  form.elements.ide_type.value = 'ANTIGRAVITY'; form.elements.enabled.checked = true;
  document.getElementById('form-title').textContent = 'Add Resource';
  document.getElementById('cancel-edit').hidden = true;
}
function editResource(id) {
  const resource = rows.find(item => item.id === id); if (!resource) return;
  resetForm(); form.elements.original_id.value = resource.id; form.elements.id.value = resource.id;
  for (const name of ['project_id', 'ide_type', 'credential_id']) form.elements[name].value = resource[name] || '';
  form.elements.enabled.checked = resource.enabled;
  document.getElementById('form-title').textContent = 'Edit Resource ' + resource.id;
  document.getElementById('cancel-edit').hidden = false;
}
async function setEnabled(id, enabled) {
  await api(`/admin/resources/${encodeURIComponent(id)}/${enabled ? 'enable' : 'disable'}`, {method:'POST'});
  await loadResources();
}
async function deleteResource(id) {
  if (!confirm(`Delete Resource "${id}"?`)) return;
  await api('/admin/resources/' + encodeURIComponent(id), {method:'DELETE'});
  await loadResources();
}
function openCredentialDialog(id) {
  credForm.reset();
  document.getElementById('credential-dialog-title').textContent =
    id ? 'Edit Credential ' + id : 'New Credential';
  document.getElementById('payload-hint').textContent = id
    ? 'Leave the payload empty to keep the stored payload unchanged.'
    : '';
  credForm.elements.id.value = id || '';
  credForm.elements.id.readOnly = Boolean(id);
  // The stored payload is never read back into the form (redacted or not).
  credForm.elements.payload.value = '';
  if (id) {
    const existing = credentials.find(item => item.id === id);
    if (existing) credForm.elements.type.value = existing.type;
  }
  credDialog.showModal();
}
document.getElementById('new-credential').addEventListener('click', () => openCredentialDialog(null));
document.getElementById('credential-cancel').addEventListener('click', () => credDialog.close());
credTbody.addEventListener('click', async event => {
  const button = event.target.closest('button[data-action]'); if (!button) return;
  const id = button.dataset.id;
  try {
    if (button.dataset.action === 'cred-edit') { openCredentialDialog(id); return; }
    if (!confirm(`Delete Credential "${id}"?`)) return;
    await api('/admin/credentials/' + encodeURIComponent(id), {method:'DELETE'});
    await loadCredentials();
  } catch (error) { message.textContent = error.message; }
});
credForm.addEventListener('submit', async event => {
  event.preventDefault();
  const data = new FormData(credForm);
  const id = String(data.get('id') || '').trim();
  const raw = String(data.get('payload') || '').trim();
  let payload = {};
  if (raw) {
    try { payload = JSON.parse(raw); }
    catch (_) { message.textContent = 'Payload must be valid JSON'; return; }
  }
  try {
    if (credForm.elements.id.readOnly) {
      if (raw) {
        await api('/admin/credentials/' + encodeURIComponent(id),
                  {method:'PATCH', body: JSON.stringify({payload})});
      }
    } else {
      await api('/admin/credentials', {method:'POST',
                body: JSON.stringify({id, type: data.get('type'), payload})});
    }
    credDialog.close();
    credForm.elements.payload.value = '';
    message.textContent = '';
    await loadCredentials();
  } catch (error) { message.textContent = error.message; }
});
document.getElementById('load').addEventListener('click', () => {
  loadResources(); loadCredentials();
});
document.getElementById('cancel-edit').addEventListener('click', resetForm);
tbody.addEventListener('click', async event => {
  const button = event.target.closest('button[data-action]'); if (!button) return;
  try {
    if (button.dataset.action === 'edit') editResource(button.dataset.id);
    else if (button.dataset.action === 'enabled') await setEnabled(button.dataset.id, button.dataset.value === 'true');
    else await deleteResource(button.dataset.id);
  } catch (error) { message.textContent = error.message; }
});
form.addEventListener('submit', async event => {
  event.preventDefault(); const data = new FormData(form); const payload = {};
  for (const [key, value] of data.entries()) if (key !== 'original_id' && value !== '') payload[key] = value;
  payload.enabled = data.get('enabled') === 'on';
  const originalId = data.get('original_id');
  try {
    if (originalId) {
      delete payload.id;
      await api('/admin/resources/' + encodeURIComponent(originalId), {method:'PATCH', body:JSON.stringify(payload)});
    }
    else await api('/admin/resources', {method:'POST', body:JSON.stringify(payload)});
    resetForm(); await loadResources();
  } catch (error) { message.textContent = error.message; }
});
</script>
</body>
</html>"""


@router.get("/resources")
async def list_resources(request: Request):
    _require_admin(request)
    return _manager(request).list_resources()


@router.get("/resources/{resource_id}")
async def get_resource(resource_id: str, request: Request):
    _require_admin(request)
    try:
        return _manager(request).serialize(
            _manager(request).get_resource(resource_id)
        )
    except ResourceManagementError as exc:
        raise _not_found(exc) from exc


@router.post("/resources", status_code=status.HTTP_201_CREATED)
async def create_resource(request: Request):
    _require_admin(request)
    payload = await _json_object(request)
    try:
        resource = await _manager(request).create_resource(payload)
        return _manager(request).serialize(resource)
    except ResourceManagementError as exc:
        raise _not_found(exc) from exc


@router.patch("/resources/{resource_id}")
async def update_resource(resource_id: str, request: Request):
    _require_admin(request)
    payload = await _json_object(request)
    try:
        resource = await _manager(request).update_resource(resource_id, payload)
        return _manager(request).serialize(resource)
    except ResourceManagementError as exc:
        raise _not_found(exc) from exc


@router.post("/resources/{resource_id}/enable")
async def enable_resource(resource_id: str, request: Request):
    _require_admin(request)
    try:
        resource = await _manager(request).set_enabled(resource_id, True)
        return _manager(request).serialize(resource)
    except ResourceManagementError as exc:
        raise _not_found(exc) from exc


@router.post("/resources/{resource_id}/disable")
async def disable_resource(resource_id: str, request: Request):
    _require_admin(request)
    try:
        resource = await _manager(request).set_enabled(resource_id, False)
        return _manager(request).serialize(resource)
    except ResourceManagementError as exc:
        raise _not_found(exc) from exc


@router.delete("/resources/{resource_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_resource(resource_id: str, request: Request):
    _require_admin(request)
    try:
        await _manager(request).delete_resource(resource_id)
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
    try:
        credential = _credential_repository(request).update_payload(
            credential_id, payload
        )
    except UnknownCredentialError as exc:
        raise _credential_http_error(exc) from exc
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


@router.delete(
    "/credentials/{credential_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_credential(credential_id: str, request: Request):
    _require_admin(request)
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
