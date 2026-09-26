<script setup lang="ts">
/**
 * Resource create/edit form.
 *
 * WEBUI-001/002 §3: this form edits *runtime* configuration only
 * (`id`, `provider`, `enabled`, `credential_id`, `project_id`, `ide_type`).
 * No long-lived credential field appears here at all: that material is
 * managed on the Credentials view and linked via `credential_id`.
 */
import { computed, ref, watch } from 'vue'
import AppModal from './AppModal.vue'
import type { Resource } from '../types/admin'

interface ResourceFormPayload {
  id?: string
  provider?: string
  enabled: boolean
  credential_id: string
  project_id: string
  ide_type: string
}

const props = defineProps<{
  open: boolean
  resource: Resource | null
  busy: boolean
  error: string
}>()

const emit = defineEmits<{
  (event: 'close'): void
  (event: 'submit', payload: ResourceFormPayload): void
}>()

const id = ref('')
const provider = ref('')
const credentialId = ref('')
const projectId = ref('')
const ideType = ref('ANTIGRAVITY')
const enabled = ref(true)

const isEditing = computed(() => props.resource !== null)

watch(
  () => [props.open, props.resource] as const,
  ([open, resource]) => {
    if (!open) return
    id.value = resource?.id ?? ''
    provider.value = resource?.provider ?? ''
    credentialId.value = resource?.credential_id ?? ''
    projectId.value = resource?.project_id ?? ''
    ideType.value = resource?.ide_type || 'ANTIGRAVITY'
    enabled.value = resource?.enabled ?? true
  },
  { immediate: true },
)

function submit() {
  if (!isEditing.value && !id.value.trim()) return
  const payload: ResourceFormPayload = {
    enabled: enabled.value,
    credential_id: credentialId.value.trim(),
    project_id: projectId.value.trim(),
    ide_type: ideType.value.trim(),
  }
  if (!isEditing.value) {
    payload.id = id.value.trim()
    if (provider.value.trim()) payload.provider = provider.value.trim()
  }
  emit('submit', payload)
}
</script>

<template>
  <AppModal
    :open="open"
    :title="isEditing ? `Edit Resource ${resource?.id}` : 'Add Resource'"
    @close="emit('close')"
  >
    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <label>
      Resource ID
      <input v-model="id" :disabled="isEditing" required autocomplete="off" />
    </label>
    <label>
      Provider
      <input
        v-model="provider"
        :disabled="isEditing"
        autocomplete="off"
        placeholder="e.g. antigravity"
      />
    </label>
    <label>
      Credential ID
      <input
        v-model="credentialId"
        autocomplete="off"
        placeholder="e.g. google-oauth-01"
      />
    </label>
    <label>
      Project ID
      <input v-model="projectId" autocomplete="off" />
    </label>
    <label>
      IDE Type
      <input v-model="ideType" autocomplete="off" />
    </label>
    <label class="checkbox">
      <input v-model="enabled" type="checkbox" /> Enabled
    </label>

    <p class="muted">
      Runtime configuration only. Long-lived credential material is managed
      under <strong>Credentials</strong> and linked via Credential ID.
    </p>

    <template #footer>
      <button type="button" @click="emit('close')">Cancel</button>
      <button type="button" class="primary" :disabled="busy" @click="submit">
        Save
      </button>
    </template>
  </AppModal>
</template>

<style scoped>
.error {
  color: var(--danger);
}

.muted {
  color: var(--muted);
  font-size: 0.9em;
}

.checkbox {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}

.checkbox input {
  width: auto;
}
</style>
