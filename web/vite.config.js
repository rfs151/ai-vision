import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const API = process.env.API || 'http://127.0.0.1:8090'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': API,
      '/media': API,
      '/ws': { target: API.replace(/^http/, 'ws'), ws: true },
    },
  },
  build: { outDir: 'dist', chunkSizeWarningLimit: 1500 },
})
