import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The API runs on 127.0.0.1:8010; proxying /api through the dev server means
// only one port (5174) has to be forwarded when working over SSH, and the
// frontend can use relative URLs.
export default defineConfig({
  plugins: [react()],
  // maplibre-gl loads its render worker via `new Worker(new URL(...))`;
  // Vite's dep pre-bundling rewrites that URL in a way the worker
  // can't resolve, so it must be excluded from optimizeDeps.
  optimizeDeps: {
    exclude: ['maplibre-gl'],
  },
  server: {
    port: 5174,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8010',
        changeOrigin: true,
        timeout: 600000,
        proxyTimeout: 600000,
      },
    },
  },
})
