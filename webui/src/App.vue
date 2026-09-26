<script setup lang="ts">
/**
 * Admin SPA shell.
 *
 * WEBUI-002 §4: the admin token is kept in a ref in this component's runtime
 * state only. It is never written to localStorage/sessionStorage/cookies nor
 * to the server, and it disappears on reload.
 */
import { computed, onMounted, ref, watch } from 'vue'
import DashboardView from './views/DashboardView.vue'
import ResourcesView from './views/ResourcesView.vue'
import CredentialsView from './views/CredentialsView.vue'
import { useAdminToken } from './api/token'

type Tab = 'dashboard' | 'resources' | 'credentials'

const { token, setToken, isConfigured } = useAdminToken()

const tab = ref<Tab>('dashboard')
const message = ref('')
const resourcesView = ref<InstanceType<typeof ResourcesView> | null>(null)
const credentialsView = ref<InstanceType<typeof CredentialsView> | null>(null)

const tabs: { id: Tab; label: string }[] = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'resources', label: 'Resources' },
  { id: 'credentials', label: 'Credentials' },
]

const currentTitle = computed(
  () => tabs.find((item) => item.id === tab.value)?.label ?? 'Admin',
)

function notify(text: string) {
  message.value = text
}

function onTokenInput(event: Event) {
  const target = event.target
  if (target instanceof HTMLInputElement) setToken(target.value)
}

async function refreshAll() {
  message.value = ''
  // WEBUI-002 §4: with no token in runtime state there is nothing to ask the
  // Admin API for yet, so skip the call instead of firing a guaranteed 401.
  if (!isConfigured.value) return
  if (tab.value === 'resources' && resourcesView.value) {
    await resourcesView.value.load()
  } else if (tab.value === 'credentials' && credentialsView.value) {
    await credentialsView.value.load()
  }
}

onMounted(() => {
  void refreshAll()
})

watch(tab, () => {
  void refreshAll()
})
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand">
        <h1>Gemini Gateway</h1>
        <span class="muted">Admin · {{ currentTitle }}</span>
      </div>
      <div class="auth">
        <label class="token">
          Admin token
          <input
            :value="token"
            type="password"
            autocomplete="off"
            placeholder="ADMIN_TOKEN"
            @input="onTokenInput"
          />
        </label>
        <button type="button" class="primary" :disabled="!isConfigured" @click="refreshAll">
          Connect
        </button>
      </div>
    </header>

    <nav class="tabs">
      <button
        v-for="item in tabs"
        :key="item.id"
        type="button"
        :class="{ active: tab === item.id }"
        @click="tab = item.id"
      >
        {{ item.label }}
      </button>
    </nav>

    <p v-if="message" class="banner" role="alert">{{ message }}</p>

    <main>
      <DashboardView v-if="tab === 'dashboard'" />
      <ResourcesView v-else-if="tab === 'resources'" ref="resourcesView" @error="notify" />
      <CredentialsView v-else ref="credentialsView" @error="notify" />
    </main>

    <footer class="muted small">
      Resources carry runtime configuration only. Long-lived credential material
      is owned by Credentials and stored through the CredentialRepository.
    </footer>
  </div>
</template>

<style scoped>
.shell {
  max-width: 1180px;
  margin: 0 auto;
  padding: 1.5rem 1rem 3rem;
}

.topbar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
  border-bottom: 1px solid var(--border);
  padding-bottom: 0.75rem;
}

.brand h1 {
  margin: 0;
  font-size: 1.4rem;
}

.auth {
  display: flex;
  align-items: flex-end;
  gap: 0.5rem;
}

.token {
  margin: 0;
  width: 220px;
}

.tabs {
  display: flex;
  gap: 0.35rem;
  margin: 1rem 0;
  flex-wrap: wrap;
}

.tabs .active {
  background: #222;
  color: #fff;
  border-color: #222;
}

.banner {
  background: #fdecea;
  border: 1px solid #f2c4c0;
  color: var(--danger);
  padding: 0.6rem 0.8rem;
  border-radius: 4px;
}

.muted {
  color: var(--muted);
}

.small {
  font-size: 0.85em;
}

main {
  padding-bottom: 1rem;
}
</style>
