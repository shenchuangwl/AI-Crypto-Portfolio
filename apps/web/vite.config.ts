import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      // Mock api-gateway (default :18080 — :8080 often taken by docker-proxy)
      '/api': {
        target: process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:18080',
        changeOrigin: true,
      },
    },
  },
})
