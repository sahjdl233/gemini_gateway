<script setup lang="ts">
/**
 * Dashboard: aggregate health at a glance (WEBUI-002 §2).
 * Reads the same endpoints as the other views -- it does not add API surface.
 */
import { computed, ref } from 'vue'
import DataState from '../components/DataState.vue'
import { listCredentials, listResources } from '../api/admin'
import type { Credential, Resource } from '../types/admin'

const resources = ref<Resource[]>([])
const credentials = ref<Credential[]>([])
const loading = ref(false)
const error = ref('')
const loaded = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [resourceList, credentialList] = await Promise.all([
      listResources(),
      listCredentials(),
    ])
    resources.value = resourceList
    credentials.value = credentialList
    loaded.value = true
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    loading.value = false
  }
}

const enabled = computed(() => resources.value.filter((r) => r.enabled).length)
const disabled = computed(() => resources.value.length - enabled.value)
const inFlight = computed(() =>
  resources.value.reduce((total, r) => total + r.in_flight, 0),
)
const requests = computed(() =>
  resources.value.reduce((total, r) => total + r.total_requests, 0),
)
const failures = computed(() =>
  resources.value.reduce((total, r) => total + r.total_failures, 0),
)
const unhealthy = computed(() =>
  resources.value.filter((r) => r.health && r.health.toUpperCase() !== 'HEALTHY').length,
)
const referenced = computed(() => credentials.value.filter((c) => c.referenced).length)
const providers = computed(() => new Set(resources.value.map((r) => r.provider)).size)
</script>

<template>
  <section>
    <header class="bar">
      <h2>Dashboard</h2>
      <button type="button" @click="load">Refresh</button>
    </header>

    <DataState
      :loading="loading"
      :error="error"
      :empty="loaded && resources.length === 0 && credentials.length === 0"
      empty-text="Nothing configured yet."
    >
      <div class="tiles">
        <div class="tile">
          <span class="label">Resources</span>
          <strong>{{ resources.length }}</strong>
          <span class="sub">{{ enabled }} enabled · {{ disabled }} disabled</span>
        </div>
        <div class="tile">
          <span class="label">Providers</span>
          <strong>{{ providers }}</strong>
          <span class="sub">distinct provider(s)</span>
        </div>
        <div class="tile">
          <span class="label">Credentials</span>
          <strong>{{ credentials.length }}</strong>
          <span class="sub">{{ referenced }} referenced</span>
        </div>
        <div class="tile">
          <span class="label">In flight</span>
          <strong>{{ inFlight }}</strong>
          <span class="sub">active requests</span>
        </div>
        <div class="tile">
          <span class="label">Requests</span>
          <strong>{{ requests }}</strong>
          <span class="sub">lifetime total</span>
        </div>
        <div class="tile">
          <span class="label">Failures</span>
          <strong>{{ failures }}</strong>
          <span class="sub">{{ unhealthy }} not healthy</span>
        </div>
      </div>

      <h3>Resource status</h3>
      <table v-if="resources.length">
        <thead>
          <tr>
            <th>ID</th>
            <th>Provider</th>
            <th>Health</th>
            <th>Enabled</th>
            <th>Cooldown</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="resource in resources" :key="resource.id">
            <td><code>{{ resource.id }}</code></td>
            <td>{{ resource.provider }}</td>
            <td>
              <span :class="{ bad: resource.health.toUpperCase() !== 'HEALTHY' }">
                {{ resource.health }}
              </span>
            </td>
            <td>{{ resource.enabled ? 'yes' : 'no' }}</td>
            <td>{{ resource.cooldown_until || 'none' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">No resources configured.</p>
    </DataState>
  </section>
</template>

<style scoped>
.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}

.bar h2 {
  margin: 0;
}

.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 0.75rem;
  margin: 1rem 0;
}

.tile {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}

.tile .label {
  color: var(--muted);
  font-size: 0.8em;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.tile strong {
  font-size: 1.6rem;
}

.tile .sub {
  color: var(--muted);
  font-size: 0.8em;
}

.bad {
  color: var(--danger);
}

.muted {
  color: var(--muted);
}

h3 {
  margin-top: 1.6rem;
}
</style>
