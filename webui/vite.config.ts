import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// WEBUI-002: the Admin SPA is served by FastAPI from `webui/dist` under the
// `/admin/` prefix, so every emitted asset URL must be absolute from that
// root (default `base` behaviour) rather than relative to the current path.
export default defineConfig({
  // WEBUI-002 §5: FastAPI serves this build from `/admin/`, so emitted asset
  // URLs must be prefixed with that base for the SPA to resolve them.
  base: '/admin/',
  plugins: [vue()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      // Dev-only: forward the unchanged Admin contract to FastAPI.
      '/admin': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
