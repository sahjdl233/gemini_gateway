<script setup lang="ts">
/**
 * Credential create/edit form -- the only place long-lived material is typed.
 *
 * WEBUI-002 §3: the payload textarea is *always* blank when the dialog opens,
 * even in edit mode. The redacted payload returned by GET is displayed read-only
 * elsewhere and is never written back into this input, so a secret can only
 * reach the API if the operator deliberately retypes it. A blank payload on
 * edit means "leave the stored payload unchanged" and sends no PATCH.
 */
import { computed, ref, watch } from 'vue'
import AppModal from './AppModal.vue'
import type { CredentialTypeValue } from '../types/admin'

const props = defineProps<{
  open: boolean
  credentialId: string | null
  credentialType: CredentialTypeValue | null
  busy: boolean
  error: string
}>()

const emit = defineEmits<{
  (event: 'close'): void
  /** Raw payload text; the parent owns JSON parsing and error reporting. */
  (event: 'submit', payload: { id: string; type: CredentialTypeValue; payloadText: string }): void
}>()

const id = ref('')
const type = ref<CredentialTypeValue>('oauth')
const payloadText = ref('')

const isEditing = computed(() => props.credentialId !== null)

watch(
  () => [props.open, props.credentialId] as const,
  ([open, credentialId]) => {
    if (!open) return
    id.value = credentialId ?? ''
    type.value = props.credentialType ?? 'oauth'
    // Never pre-fill a secret: the stored payload is unknown to the client.
    payloadText.value = ''
  },
  { immediate: true },
)

function submit() {
  emit('submit', {
    id: id.value.trim(),
    type: type.value,
    payloadText: payloadText.value.trim(),
  })
}
</script>

<template>
  <AppModal
    :open="open"
    :title="isEditing ? `Edit Credential ${credentialId}` : 'New Credential'"
    @close="emit('close')"
  >
    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <label>
      Credential ID
      <input v-model="id" :disabled="isEditing" required autocomplete="off" />
    </label>
    <label>
      Type
      <!-- PATCH only replaces the payload; the stored type is immutable. -->
      <select v-model="type" :disabled="isEditing">
        <option value="none">none</option>
        <option value="api_key">api_key</option>
        <option value="oauth">oauth</option>
      </select>
    </label>
    <label>
      Payload JSON
      <textarea
        v-model="payloadText"
        rows="8"
        spellcheck="false"
        placeholder='{&quot;refresh_token&quot;: &quot;...&quot;}'
      ></textarea>
    </label>

    <p class="muted">
      <template v-if="isEditing">
        Stored values are never displayed. Leave this blank to keep the current
        payload unchanged, or paste a new payload to replace it.
      </template>
      <template v-else>
        Stored through the CredentialRepository. Values are never returned in
        plaintext once saved.
      </template>
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
</style>
