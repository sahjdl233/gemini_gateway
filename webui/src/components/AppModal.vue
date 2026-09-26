<script setup lang="ts">
/** Minimal accessible modal (no UI framework, per WEBUI-002 §1). */
defineProps<{
  open: boolean
  title: string
}>()

const emit = defineEmits<{ (event: 'close'): void }>()
</script>

<template>
  <div v-if="open" class="backdrop" @click.self="emit('close')">
    <div class="modal" role="dialog" aria-modal="true" :aria-label="title">
      <header>
        <h3>{{ title }}</h3>
        <button type="button" aria-label="Close" @click="emit('close')">✕</button>
      </header>
      <div class="body">
        <slot />
      </div>
      <footer>
        <slot name="footer" />
      </footer>
    </div>
  </div>
</template>

<style scoped>
.backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1rem;
  z-index: 50;
}

.modal {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 6px;
  width: 100%;
  max-width: 480px;
  max-height: 90vh;
  overflow: auto;
}

header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.75rem 1rem;
  border-bottom: 1px solid var(--border);
}

header h3 {
  margin: 0;
  font-size: 1.05rem;
}

.body {
  padding: 0.25rem 1rem 0.5rem;
}

footer {
  padding: 0.5rem 1rem 0.9rem;
  border-top: 1px solid var(--border);
  text-align: right;
}
</style>
