import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Built assets are served by the agent (FastAPI) under /app-assets/,
// index.html is served at /research. Relative base keeps it robust.
export default defineConfig({
  base: './',
  plugins: [react(), tailwindcss()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  optimizeDeps: {
    /* Monaco is huge and internally references its own files by relative URL;
       pre-bundling it breaks those references. Left as a source dep so the
       worker and language files resolve relative to the bundle. */
    exclude: ['monaco-editor'],
  },
  server: {
    port: 5173,
    host: '127.0.0.1',
    proxy: {
      // 127.0.0.1, not localhost: on Windows `localhost` resolves to ::1
      // first, and the agent binds IPv4 only  which produced a dev-only
      // "API is down" that never reproduced against the served build.
      '/api': { target: 'http://127.0.0.1:8500', changeOrigin: true },
    },
  },
})
