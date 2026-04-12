import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// When built, the SPA is served by Django at /ow/ with assets under
// /static/patient-frontend/. The base path tells Vite to emit asset
// URLs prefixed with /static/patient-frontend/ instead of /.
export default defineConfig({
  plugins: [react()],
  base: '/static/patient-frontend/',
  build: {
    outDir: '../core/static/patient-frontend',
    emptyOutDir: true,
    assetsDir: 'assets',
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
      '/o': 'http://localhost:8000',
      '/fhir': 'http://localhost:8000',
    },
  },
})
