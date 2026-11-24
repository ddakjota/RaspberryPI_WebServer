import { defineConfig } from 'vite'

export default defineConfig({
  server: { host: 'localhost', proxy: { '/api': 'http://localhost:8000' } },
})
