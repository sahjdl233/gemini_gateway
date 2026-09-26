<script setup lang="ts">
defineProps<{
  loading: boolean
  error: string
  empty: boolean
  emptyText: string
}>()
</script>

<template>
  <p v-if="error" class="error" role="alert">{{ error }}</p>
  <p v-else-if="loading" class="muted">Loading…</p>
  <p v-else-if="empty" class="muted">{{ emptyText }}</p>
  <slot v-else />
</template>

<style scoped>
.error {
  color: var(--danger);
}

.muted {
  color: var(--muted);
}
</style>
