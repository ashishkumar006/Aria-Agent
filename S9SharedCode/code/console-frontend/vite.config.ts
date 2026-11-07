import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The agent serves the built shell at every route and its chunks under
// /assets/ (agent_server.py's `@app.get("/assets/{path}")`). The base has to be
// absolute for that to work: with './', `index.html` emits `./assets/app.js`,
// which the browser resolves against the CURRENT directory — so a nested route
// like /authoring/focus asks for /authoring/assets/app.js, the fallback answers
// 404 by design, and the console loads as a blank page with no error anywhere
// in it. Root-absolute URLs resolve the same from any depth, which is also what
// makes the trailing-slash rebase the fallback documents there harmless.
export default defineConfig({
  base: '/',
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
