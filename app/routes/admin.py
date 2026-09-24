"""Authenticated Antigravity Resource management API and lightweight UI."""

from __future__ import annotations

import hmac
import json
import os
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from app.management import ResourceManagementError, ResourceManager


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
    body{font:15px system-ui,sans-serif;max-width:1120px;margin:2rem auto;padding:0 1rem;color:#222}
    table{border-collapse:collapse;width:100%}th,td{padding:.55rem;border-bottom:1px solid #ddd;text-align:left}
    input{width:100%;box-sizing:border-box;padding:.45rem;margin:.2rem 0}
    button{cursor:pointer;margin:.15rem;padding:.35rem .6rem}
    form{border:1px solid #ddd;padding:1rem;margin:1rem 0;max-width:720px}
    label{display:block;margin:.45rem 0}.muted{color:#666}.message{min-height:1.4em}
    .disabled{opacity:.65}dialog{border:1px solid #aaa;padding:1rem;max-width:420px}
  </style>
</head>
<body>
  <h1>Gemini Gateway</h1>
  <p class="muted">Antigravity Resource management</p>
  <p>
    <label>Admin token
      <input id="token" type="password" autocomplete="off" size="40">
    </label>
    <button id="load" type="button">Load resources</button>
  </p>
  <div id="message" class="message"></div>
  <table>
    <thead><tr>
      <th>ID</th><th>Provider</th><th>Enabled</th><th>Health</th><th>Cooldown</th>
      <th>In flight</th><th>Requests</th><th>Failures</th><th>Project</th><th>Actions</th>
    </tr></thead>
    <tbody id="resources"></tbody>
  </table>

  <h2 id="form-title">Add Resource</h2>
  <form id="resource-form">
    <input type="hidden" name="original_id">
    <label>Resource ID<input name="id" required></label>
    <label>Access Token<input name="access_token" type="password" autocomplete="new-password"></label>
    <label>Refresh Token<input name="refresh_token" type="password" autocomplete="new-password"></label>
    <label>Client ID<input name="client_id" type="password" autocomplete="off"></label>
    <label>Client Secret<input name="client_secret" type="password" autocomplete="new-password"></label>
    <label>Project ID<input name="project_id"></label>
    <label>IDE Type<input name="ide_type" value="ANTIGRAVITY"></label>
    <label><input type="checkbox" name="enabled" checked> Enabled</label>
    <button type="submit">Save</button>
    <button id="cancel-edit" type="button" hidden>Cancel edit</button>
  </form>

<script>
const token = document.getElementById('token');
const message = document.getElementById('message');
const form = document.getElementById('resource-form');
const tbody = document.getElementById('resources');
let rows = [];

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
function render() {
  tbody.innerHTML = rows.map(r => `<tr class="${r.enabled ? '' : 'disabled'}">
    <td>${esc(r.id)}</td><td>${esc(r.provider)}</td><td>${r.enabled}</td>
    <td>${esc(r.health)}</td><td>${esc(r.cooldown_until || 'none')}</td>
    <td>${r.in_flight}</td><td>${r.total_requests}</td><td>${r.total_failures}</td>
    <td>${esc(r.project_id || '')}</td>
    <td>
      <button data-action="enabled" data-id="${esc(r.id)}" data-value="${!r.enabled}">${r.enabled ? 'Disable' : 'Enable'}</button>
      <button data-action="edit" data-id="${esc(r.id)}">Edit</button>
      <button data-action="delete" data-id="${esc(r.id)}">Delete</button>
    </td></tr>`).join('');
}
async function loadResources() {
  try { rows = await api('/admin/resources'); render(); message.textContent = ''; }
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
  for (const name of ['project_id', 'ide_type']) form.elements[name].value = resource[name] || '';
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
document.getElementById('load').addEventListener('click', loadResources);
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
