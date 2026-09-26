/**
 * WEBUI-001/002 security boundary, mirrored on the client.
 *
 * `Resource` is a *runtime* object: it carries scheduling configuration and a
 * `credential_id` pointer only. Long-lived credential material belongs to
 * `Credential` and is never part of a Resource payload -- the backend rejects
 * those fields with 400, and this type makes them unrepresentable in the UI.
 */

export interface Resource {
  id: string
  provider: string
  enabled: boolean
  credential_id: string | null
  health: string
  cooldown_until: string | null
  in_flight: number
  total_requests: number
  total_failures: number
  project_id?: string | null
  ide_type?: string | null
}

/** The exact set of fields the Admin Resource API accepts (WEBUI-001 §1). */
export interface ResourceWritable {
  id?: string
  provider?: string
  enabled?: boolean
  credential_id?: string | null
  project_id?: string | null
  ide_type?: string | null
}

export type CredentialTypeValue = 'none' | 'api_key' | 'oauth'

/** A redacted payload: secret-shaped values arrive masked as `***`. */
export type RedactedPayload = Record<string, unknown>

export interface Credential {
  id: string
  type: CredentialTypeValue
  /** Always redacted. Real secrets never reach the browser. */
  payload: RedactedPayload
  created_at: string
  updated_at: string
  /** True when a live Resource still points at this credential (AUTH-013). */
  referenced: boolean
}

export interface CredentialCreate {
  id: string
  type: CredentialTypeValue
  payload: Record<string, unknown>
}

export interface CredentialUpdate {
  payload: Record<string, unknown>
}
