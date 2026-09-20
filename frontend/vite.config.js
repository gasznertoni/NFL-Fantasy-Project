import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vercel and `npm run dev` both serve from the root, so `base` resolves to '/'
// and nothing sets VITE_BASE today. The override stays as an escape hatch: a
// host that serves the app from a SUBPATH needs a matching base or every asset
// and fixture 404s.
//
// The GitHub Pages workflow that used to set VITE_BASE was deleted 2026-09-20.
// It had failed all 11 of its runs on main -- Pages was never enabled on this
// private repo, and Vercel had been doing the deploy all along through its own
// GitHub App.
//
// This is load-bearing beyond assets: lib/api.js builds all eight fixture URLs
// from import.meta.env.BASE_URL, which Vite derives from this value. Get it
// wrong and the app renders an empty shell with no data and no error.
export default defineConfig({
  base: process.env.VITE_BASE || '/',
  plugins: [react()],
})
