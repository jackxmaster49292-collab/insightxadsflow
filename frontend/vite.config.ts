import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 8080,
    // The SPA and API are same-origin in production (nginx); this mirrors that
    // locally so the session cookie's SameSite=Strict behaves identically.
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: false },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
})
