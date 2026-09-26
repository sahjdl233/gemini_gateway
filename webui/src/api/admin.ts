/**
 * Single client for the existing `/admin/*` contract.
 *
 * WEBUI-002 §4: the contract is consumed as-is; nothing here redesigns the
 * API. The admin token is held only in the page's in-memory runtime state
 * (see `useAdminToken`) and is never written to config.yaml, localStorage or
 * the backend database.
 */
import type {
  Credential,
  CredentialCreate,
  CredentialUpdate,
  Resource,
  ResourceWritable,
} from '../types/admin'

export const ADMIN_BASE = '/admin'

/** Raised for any non-2xx Admin response, carrying the backend's message. */
export class AdminApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'AdminApiError'
    this.status = status
  }
}

type TokenReader = () => string

let readToken: TokenReader = () => ''

/** Registered by the app shell so the token stays in runtime state only. */
export function setTokenReader(reader: TokenReader): void {
  readToken = reader
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Authorization', `Bearer ${readToken()}`)
  if (init.body !== undefined) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(`${ADMIN_BASE}${path}`, { ...init, headers })

  if (response.status === 204) {
    return undefined as T
  }

  const text = await response.text()
  const parsed = text ? (JSON.parse(text) as unknown) : null

  if (!response.ok) {
    // The backend already keeps secrets out of its error bodies; surface the
    // message verbatim so operators see field-level guidance (e.g. which
    // credential field was rejected).
    const detail =
      parsed && typeof parsed === 'object' && 'detail' in parsed
        ? String((parsed as { detail: unknown }).detail)
        : text
    throw new AdminApiError(response.status, detail)
  }

  return parsed as T
}

// -- Resources ------------------------------------------------------------------

export function listResources(): Promise<Resource[]> {
  return request<Resource[]>('/resources')
}

export function createResource(payload: ResourceWritable): Promise<Resource> {
  return request<Resource>('/resources', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function updateResource(
  id: string,
  payload: ResourceWritable,
): Promise<Resource> {
  return request<Resource>(`/resources/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function setResourceEnabled(id: string, enabled: boolean): Promise<Resource> {
  return request<Resource>(
    `/resources/${encodeURIComponent(id)}/${enabled ? 'enable' : 'disable'}`,
    { method: 'POST' },
  )
}

export function deleteResource(id: string): Promise<void> {
  return request<void>(`/resources/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

// -- Credentials ----------------------------------------------------------------

export function listCredentials(): Promise<Credential[]> {
  return request<Credential[]>('/credentials')
}

export function getCredential(id: string): Promise<Credential> {
  return request<Credential>(`/credentials/${encodeURIComponent(id)}`)
}

export function createCredential(payload: CredentialCreate): Promise<Credential> {
  return request<Credential>('/credentials', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function updateCredential(
  id: string,
  payload: CredentialUpdate,
): Promise<Credential> {
  return request<Credential>(`/credentials/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function deleteCredential(id: string): Promise<void> {
  return request<void>(`/credentials/${encodeURIComponent(id)}`, { method: 'DELETE' })
}
