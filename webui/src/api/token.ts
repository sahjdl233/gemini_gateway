/**
 * WEBUI-002 §4: the admin token lives only in the current page's runtime
 * state. It is never persisted to localStorage/sessionStorage/cookies, never
 * sent anywhere except the Admin API as a bearer header, and is cleared on
 * reload by construction.
 */
import { computed, ref, readonly, type Ref } from 'vue'
import { setTokenReader } from './admin'

const token: Ref<string> = ref('')

export function useAdminToken() {
  setTokenReader(() => token.value)

  return {
    token: readonly(token),
    setToken(value: string) {
      token.value = value
    },
    clear() {
      token.value = ''
    },
    // A computed (not a plain getter) so the shell stays reactive to it.
    isConfigured: computed(() => token.value.trim().length > 0),
  }
}
