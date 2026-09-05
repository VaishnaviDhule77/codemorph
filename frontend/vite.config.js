import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev-mode proxy: `npm run dev` on :5173 forwards /api to the FastAPI
// backend on :8000. Production builds are served BY the backend (see
// backend/api/app.py, which mounts frontend/dist at "/") -- no proxy needed.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})