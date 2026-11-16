'use strict'

// Startup ordering for the ready phase.
//
// The startup contract puts the first window before slow ready-time services: a page that
// is waiting on a loopback listener or a network probe leaves the user staring
// at no window at all. The OAuth callback listener retries on demand
// (`ensureCallbackServer`), so starting it need not gate the first paint.
//
// Pure on purpose: the Electron ready handler passes the real functions in, and
// the unit test can prove the order without loading Electron.

/**
 * Run the ready-phase steps, starting the OAuth listener without waiting for it.
 *
 * @param {object} steps
 * @param {() => unknown} steps.startOAuth   start (or retry) the loopback listener
 * @param {() => void} steps.registerIpc     install IPC handlers
 * @param {() => void} steps.installMenu     install the application menu
 * @param {() => void} steps.openWindows     create/show the launch windows
 * @param {() => number} [steps.now]         monotonic-ish clock for marks
 * @param {() => number} [steps.now]         monotonic-ish clock for marks
 * @param {(err: unknown) => void} [steps.onError]
 * @param {boolean} [steps.deferOAuth]       opt-in: do not wait for the listener before the window
 * @returns {Promise<{marks: Record<string, number>, oauthReady: Promise<unknown>}>}
 */
async function startShell({
  startOAuth,
  registerIpc,
  installMenu,
  openWindows,
  now = () => Date.now(),
  onError = () => {},
  deferOAuth = false,
}) {
  const marks = { ready: now() }
  let oauthReady
  try {
    oauthReady = Promise.resolve(startOAuth()).catch((err) => {
      onError(err)
      return null
    })
  } catch (err) {
    // A synchronous throw is the same failure as a rejected listen: log it and
    // let the window open. The page can still call `beginMcpOAuth`, which opens
    // the listener on demand.
    onError(err)
    oauthReady = Promise.resolve(null)
  }
  if (!deferOAuth) await oauthReady
  registerIpc()
  marks.ipc = now()
  installMenu()
  marks.menu = now()
  openWindows()
  marks.windows = now()
  return { marks, oauthReady }
}

module.exports = { startShell }
