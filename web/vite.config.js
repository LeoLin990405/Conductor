import { defineConfig, loadEnv } from 'vite'
import { reactPlugins } from './scripts/react-plugins.ts'
import path from 'path'
import { criticalPathGroups } from './scripts/chunking.ts'
import { localePreload } from './scripts/locale-preload.ts'
import { monacoVersionDefine } from './scripts/monaco-version.ts'
import { pdfjsData } from './scripts/vite-plugins/pdfjsData.ts'
import { skipEntryPreload } from './scripts/vite-plugins/skipEntryPreload.ts'
import { versionManifest } from './scripts/vite-plugins/versionManifest.ts'

const DEFAULT_BACKEND_PORT = '8000'

/**
 * The `/api/v1` and `/ws/v1` proxy target for `vite dev` and `vite preview`.
 * @param {Record<string, string | undefined>} viteEnv
 * @param {Record<string, string | undefined>} processEnv
 * @returns {string}
 */
export function proxyBackend(viteEnv, processEnv) {
  const explicit = (viteEnv.VITE_PROXY_BACKEND || '').trim()
  if (explicit) return explicit
  const raw = (processEnv.CONDUCTOR_BACKEND_PORT || '').trim()
  const port = /^[0-9]+$/.test(raw) && Number(raw) > 0 && Number(raw) < 65536
    ? raw
    : DEFAULT_BACKEND_PORT
  return `http://127.0.0.1:${port}`
}

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  // Load VITE_-prefixed vars from .env files (.env, .env.local, …) so they can
  // be seeded on disk instead of passed inline. loadEnv also merges matching
  // process.env entries, so `VITE_FOO=bar pnpm dev` keeps working too.
  const env = loadEnv(mode, process.cwd())
  // `cond up` passes VITE_PROXY_BACKEND for the backend it started. Unset, the
  // proxy follows CONDUCTOR_BACKEND_PORT (cond's backend port setting), so a
  // console on a non-default port never falls through to another backend.
  const backendTarget = proxyBackend(env, process.env)
  // Only honor a well-formed port; a non-numeric VITE_HMR_CLIENT_PORT would
  // otherwise yield NaN and silently break the HMR socket.
  const hmrClientPort = Number(env.VITE_HMR_CLIENT_PORT)
  const hasHmrClientPort = Number.isFinite(hmrClientPort) && hmrClientPort > 0
  // Extra Host headers the dev server accepts, for tunnels (ngrok, Cloudflare)
  // that front it under their own hostname. Comma-separated — kept in .env so a
  // tunnel host never needs a local edit to this file.
  const allowedHosts = (env.VITE_DEV_ALLOWED_HOSTS || '')
    .split(',')
    .map((host) => host.trim())
    .filter(Boolean)

  return {
    base: env.VITE_CDN_BASE || '/',
    define: monacoVersionDefine(import.meta.dirname),
    plugins: [
      ...reactPlugins(),
      versionManifest(),
      skipEntryPreload(),
      pdfjsData(import.meta.dirname),
      localePreload(path.resolve(import.meta.dirname, 'src/locales')),
    ],
    resolve: {
      // `@/` and the tests' `@e2e/` come from the tsconfig projects' `paths`,
      // each file resolving through the project that owns it.
      tsconfigPaths: true,
      alias: [
        // pdf.js 6's default build calls Map#getOrInsertComputed and Math.sumPrecise
        // unguarded (Chrome 145, Safari 26.2, Firefox 144). Its legacy build carries
        // the polyfills. The regex matches only react-pdf's bare import; the viewer
        // takes the matching legacy worker by path.
        { find: /^pdfjs-dist$/, replacement: 'pdfjs-dist/legacy/build/pdf.mjs' },
      ],
    },
    build: {
      // The raw warning budget is 900 kB because the only chunks above Vite's
      // 500 kB default are lazy readers: ExcelJS (851 kB raw / 237 kB gzip),
      // pdf.js (658/197), markdown (614/183) and the entry (556/179). The
      // critical-path gate in scripts/check-critical-path.mjs still measures
      // the first-paint budget at 378 kB gzip, so this budget does not relax
      // what a cold visit downloads.
      chunkSizeWarningLimit: 900,
      rolldownOptions: {
        // Explicit single entry: dev-only harness pages (e.g. intro-preview.html)
        // must never ship in the production build, even if a future Vite version
        // or multi-page config change starts picking up root .html files.
        input: path.resolve(import.meta.dirname, 'index.html'),
        output: {
          // Chunk groups and their priorities: scripts/chunking.ts.
          codeSplitting: { groups: criticalPathGroups },
        },
      },
    },
    server: {
      host: '127.0.0.1',
      // Unset leaves Vite's default host checking in place.
      allowedHosts: allowedHosts.length ? allowedHosts : undefined,
      watch: {
        // The docker-compose pnpm store volume sits inside the project root and
        // holds ~40k files; watching it costs one inotify watch per file, enough
        // to exhaust a Linux host's per-user limit.
        ignored: ['**/.pnpm-store/**'],
        // macOS hosts run the container FS through colima/lima, whose mount
        // does not forward inotify events — without polling, hot reload never
        // fires for edits made on the host.
        usePolling: true,
        interval: 800,
      },
      // When served behind the nginx dev proxy (oss.localhost etc.), the HMR
      // WebSocket must dial the proxy port, not the Vite port. Seed
      // VITE_HMR_CLIENT_PORT (e.g. =80) in .env.local, or pass it inline.
      // Unset leaves Vite's default HMR behavior untouched (dev-only; ignored
      // by `vite build`).
      //
      // `path` moves the HMR socket off "/" so it doesn't collide with the
      // proxy's `location = /` session redirect (/ → /home|/app), which would
      // 302 the upgrade and surface as "WebSocket closed without opened". The
      // non-root path falls through nginx's `location /` to this Vite server.
      hmr: hasHmrClientPort
        ? { clientPort: hmrClientPort, path: '/vite-hmr' }
        : undefined,
      proxy: {
        '/api/v1': {
          target: backendTarget,
          changeOrigin: true,
        },
        '/ws/v1': {
          target: backendTarget.replace(/^http/, 'ws'),
          ws: true,
        },
      },
      // `cors` stays at Vite's default, which answers only loopback origins,
      // `*.localhost` included (the dev proxy's worktree hosts). `true` let any
      // page open in the developer's browser read this server's responses.
    },
  }
})
