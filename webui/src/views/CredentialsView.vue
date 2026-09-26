<script setup lang="ts">
/**
 * Credentials: the owner of long-lived credential material (WEBUI-002 §2/§3).
 *
 * Every row renders the *redacted* payload exactly as returned by the API.
 * That value is never fed back into the editor: opening Edit always shows an
 * empty textarea, and a PATCH is issued only when the operator types a new
 * payload. Referenced credentials are protected by the backend (AUTH-013 ->
 * 409), which this view surfaces as a message rather than a silent failure.
 */
import { ref } from 'vue'
import CredentialEditor from '../components/CredentialEditor.vue'
import DataState from '../components/DataState.vue'
import {
  createCredential,
  deleteCredential,
  listCredentials,
  updateCredential,
} from '../api/admin'
import type { Credential, CredentialTypeValue } from '../types/admin'

const emit = defineEmits<{ (event: 'error', message: string): void }>()

const credentials = ref<Credential[]>([])
const loading = ref(false)
const busy = ref(false)
const loadError = ref('')
const editorOpen = ref(false)
const editorError = ref('')
const editingId = ref<string | null>(null)
const editingType = ref<CredentialTypeValue | null>(null)

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    credentials.value = await listCredentials()
  } catch (error) {
    loadError.value = message(error)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingId.value = null
  editingType.value = null
  editorError.value = ''
  editorOpen.value = true
}

function openEdit(credential: Credential) {
  editingId.value = credential.id
  editingType.value = credential.type
  editorError.value = ''
  editorOpen.value = true
}

function closeEditor() {
  editorOpen.value = false
  editorError.value = ''
}

async function save(payload: { id: string; type: CredentialTypeValue; payloadText: string }) {
  editorError.value = ''

  // Edit mode with an untouched (blank) payload means "no change": do not
  // send a PATCH at all.
  if (editingId.value && !payload.payloadText) {
    closeEditor()
    return
  }

  let parsed: Record<string, unknown>
  try {
    const value: unknown = payload.payloadText
      ? JSON.parse(payload.payloadText)
      : {}
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
      editorError.value = 'Payload must be a JSON object.'
      return
    }
    parsed = value as Record<string, unknown>
  } catch {
    // Never echo the submitted text back; it may contain a secret.
    editorError.value = 'Payload must be valid JSON.'
    return
  }

  busy.value = true
  try {
    if (editingId.value) {
      await updateCredential(editingId.value, { payload: parsed })
    } else {
      await createCredential({ id: payload.id, type: payload.type, payload: parsed })
    }
    closeEditor()
    await load()
  } catch (error) {
    editorError.value = message(error)
  } finally {
    busy.value = false
  }
}

async function remove(credential: Credential) {
  if (!window.confirm(`Delete Credential "${credential.id}"?`)) return
  busy.value = true
  try {
    await deleteCredential(credential.id)
    await load()
  } catch (error) {
    // AUTH-013: referenced credentials come back as 409; make that explicit.
    emit('error', message(error))
  } finally {
    busy.value = false
  }
}

function isEmptyPayload(payload: Record<string, unknown>): boolean {
  return Object.keys(payload).length === 0
}

defineExpose({ load })
</script>

<template>
  <section>
    <header class="bar">
      <h2>Credentials</h2>
      <div>
        <button type="button" @click="load">Refresh</button>
        <button type="button" class="primary" @click="openCreate">New Credential</button>
      </div>
    </header>

    <p class="muted">
      Managed through the CredentialRepository. Payloads are always redacted on
      read; a stored secret is never rendered back into this page.
    </p>

    <DataState
      :loading="loading"
      :error="loadError"
      :empty="credentials.length === 0"
      empty-text="No credentials stored."
    >
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Type</th>
            <th>Payload (redacted)</th>
            <th>Referenced</th>
            <th>Created</th>
            <th>Updated</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="credential in credentials" :key="credential.id">
            <td><code>{{ credential.id }}</code></td>
            <td>{{ credential.type }}</td>
            <td>
              <code v-if="!isEmptyPayload(credential.payload)">
                {{ JSON.stringify(credential.payload) }}
              </code>
              <span v-else class="muted">empty</span>
            </td>
            <td>{{ credential.referenced ? 'yes' : 'no' }}</td>
            <td>{{ credential.created_at }}</td>
            <td>{{ credential.updated_at }}</td>
            <td class="actions">
              <button type="button" :disabled="busy" @click="openEdit(credential)">
                Edit
              </button>
              <button
                type="button"
                class="danger"
                :disabled="busy"
                @click="remove(credential)"
              >
                Delete
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </DataState>

    <CredentialEditor
      :open="editorOpen"
      :credential-id="editingId"
      :credential-type="editingType"
      :busy="busy"
      :error="editorError"
      @close="closeEditor"
      @submit="save"
    />
  </section>
</template>

<style scoped>
.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
}

.bar h2 {
  margin: 0;
}

.muted {
  color: var(--muted);
  font-size: 0.9em;
}

.actions {
  white-space: nowrap;
}
</style>
