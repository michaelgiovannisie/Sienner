import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In dev, Vite proxies /api to the backend so the browser sees one origin and
// CORS never enters the picture. In production Caddy does the same job.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.SIENNER_API_URL || 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
