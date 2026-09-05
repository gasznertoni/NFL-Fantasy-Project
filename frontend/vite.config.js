import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// GitHub Pages serves a project site from a subpath
// (gasznertoni.github.io/NFL-Fantasy-Project/), so the build needs a matching
// `base` or every asset and fixture 404s. Vercel and `npm run dev` serve from
// the root and want '/'. Rather than hardcode either, the Pages workflow sets
// VITE_BASE and everything else falls back to root.
//
// This is load-bearing beyond assets: lib/api.js builds all eight fixture URLs
// from import.meta.env.BASE_URL, which Vite derives from this value. Get it
// wrong and the app renders an empty shell with no data and no error.
export default defineConfig({
  base: process.env.VITE_BASE || '/',
  plugins: [react()],
})
