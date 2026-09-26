<script setup lang="ts">
/**
 * Resources: runtime configuration and scheduling state only (WEBUI-002 §2).
 * Columns mirror the backend `serialize()` contract exactly; no credential
 * material is fetched, shown or editable here.
 */
import { ref } from 'vue'
import DataState from '../components/DataState.vue'
import ResourceEditor from '../components/ResourceEditor.vue'
import {
  createResource,
  deleteResource,
  listResources,
  setResourceEnabled,
  updateResource,
} from '../api/admin'
import type { Resource, ResourceWritable } from '../types/admin'

const emit = defineEmits<{ (event: 'error', message: string): void }>()

const resources = ref<Resource[]>([])
const loading = ref(false)
const busy = ref(false)
const loadError = ref('')
const editorOpen = ref(false)
const editorError = ref('')
const editing = ref<Resource | null>(null)

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    resources.value = await listResources()
  } catch (error) {
    loadError.value = message(error)
  } finally {
    loading.value = false
  }
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function openCreate() {
  editing.value = null
  editorError.value = ''
  editorOpen.value = true
}

function openEdit(resource: Resource) {
  editing.value = resource
  editorError.value = ''
  editorOpen.value = true
}

function closeEditor() {
  editorOpen.value = false
  editorError.value = ''
}

async function save(payload: ResourceWritable) {
  busy.value = true
  editorError.value = ''
  try {
    if (editing.value) {
      await updateResource(editing.value.id, payload)
    } else {
      await createResource(payload)
    }
    closeEditor()
    await load()
  } catch (error) {
    editorError.value = message(error)
  } finally {
    busy.value = false
  }
}

async function toggle(resource: Resource) {
  busy.value = true
  try {
    await setResourceEnabled(resource.id, !resource.enabled)
    await load()
  } catch (error) {
    emit('error', message(error))
  } finally {
    busy.value = false
  }
}

async function remove(resource: Resource) {
  if (!window.confirm(`Delete Resource "${resource.id}"?`)) return
  busy.value = true
  try {
    await deleteResource(resource.id)
    await load()
  } catch (error) {
    emit('error', message(error))
  } finally {
    busy.value = false
  }
}

defineExpose({ load })
</script>

<template>
  <section>
    <header class="bar">
      <h2>Resources</h2>
      <div>
        <button type="button" @click="load">Refresh</button>
        <button type="button" class="primary" @click="openCreate">Add Resource</button>
      </div>
    </header>

    <p class="muted">
      Runtime configuration and scheduling only. Long-lived credential
      material is owned by a <strong>Credential</strong> and reached through
      <code>credential_id</code>.
    </p>

    <DataState
      :loading="loading"
      :error="loadError"
      :empty="resources.length === 0"
      empty-text="No resources configured."
    >
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Provider</th>
            <th>Credential</th>
            <th>Enabled</th>
            <th>Health</th>
            <th>Cooldown</th>
            <th>In flight</th>
            <th>Requests</th>
            <th>Failures</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="resource in resources" :key="resource.id">
            <td><code>{{ resource.id }}</code></td>
            <td>{{ resource.provider }}</td>
            <td>
              <code v-if="resource.credential_id">{{ resource.credential_id }}</code>
              <span v-else class="muted">—</span>
            </td>
            <td>{{ resource.enabled ? 'yes' : 'no' }}</td>
            <td>{{ resource.health }}</td>
            <td>{{ resource.cooldown_until || 'none' }}</td>
            <td>{{ resource.in_flight }}</td>
            <td>{{ resource.total_requests }}</td>
            <td>{{ resource.total_failures }}</td>
            <td class="actions">
              <button type="button" :disabled="busy" @click="toggle(resource)">
                {{ resource.enabled ? 'Disable' : 'Enable' }}
              </button>
              <button type="button" :disabled="busy" @click="openEdit(resource)">
                Edit
              </button>
              <button
                type="button"
                class="danger"
                :disabled="busy"
                @click="remove(resource)"
              >
                Delete
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </DataState>

    <ResourceEditor
      :open="editorOpen"
      :resource="editing"
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
