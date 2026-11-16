'use strict'

// Desktop readiness / deep-link / bridge lifecycle state.
//
// The desktop startup contract has three
// behaviours the shell needs regardless of which remote app it hosts:
//
//   1. a readiness call the page can await, returning only the origin it may use;
//   2. initial deep links consumed once, never replayed by a reload;
//   3. a per-webContents bridge handshake that proves IPC survived a reload and
//      does not duplicate handlers.
//
// This module owns the state; main owns the Electron effects and the tests drive
// it directly, without an Electron runtime.

const STARTING = 'starting'
const READY = 'ready'

function createShellBridge({ now = () => Date.now(), readyTimeoutMs = 120_000 } = {}) {
  let serverUrl = null
  let oauthListener = 'pending'
  let error = null
  let startedAt = now()
  const waiters = new Set()
  const initialLinks = []
  const reloads = new Map()
  const listeners = new Set()

  function snapshot() {
    return {
      state: serverUrl ? READY : STARTING,
      serverUrl,
      oauthListener,
      error,
      startedAt,
      // A reload count lets the page distinguish a fresh document from a
      // bridge that has been re-established after a reload; it is not a page
      // counter and does not change on in-page navigation.
      reloads: Object.fromEntries(reloads),
    }
  }

  function emit() {
    const value = snapshot()
    for (const listener of listeners) {
      try { listener(value) } catch { /* a listener cannot break the shell */ }
    }
    return value
  }

  function settle() {
    if (!serverUrl && !error) return
    const value = snapshot()
    for (const waiter of [...waiters]) {
      clearTimeout(waiter.timer)
      waiters.delete(waiter)
      waiter.resolve(value)
    }
  }

  function markServer(origin) {
    if (!origin) return snapshot()
    serverUrl = String(origin)
    error = null
    settle()
    return emit()
  }

  function markOAuthListener(state, reason) {
    if (!['ready', 'failed'].includes(state)) throw new TypeError(`unknown OAuth listener state ${state}`)
    oauthListener = state
    if (state === 'failed') error = reason ? String(reason) : 'OAuth callback listener did not start'
    settle()
    return emit()
  }

  /**
   * Resolve once the shell has a server origin (or fail after the timeout).
   * The timeout result stays unconfigured/starting; it does not silently claim
   * readiness.
   */
  function awaitReady() {
    if (serverUrl || error) return Promise.resolve(snapshot())
    return new Promise((resolve) => {
      const waiter = { resolve, timer: null }
      waiter.timer = setTimeout(() => {
        waiters.delete(waiter)
        resolve({ ...snapshot(), error: 'timed out waiting for a server origin' })
      }, readyTimeoutMs)
      // A waiter that settles early must not keep the process alive.
      if (typeof waiter.timer.unref === 'function') waiter.timer.unref()
      waiters.add(waiter)
    })
  }

  /** Queue a deep link received before the page could consume it; newest last. */
  function queueDeepLink(raw) {
    if (typeof raw !== 'string' || !raw) return
    initialLinks.push(raw)
  }

  /** One-shot: hand the queued links to the page and clear them. */
  function consumeInitialDeepLinks() {
    const links = initialLinks.splice(0, initialLinks.length)
    return links
  }

  /** Record one bridge handshake per webContents; reloads are the repeat calls. */
  function noteBridgeReady(webContentsId) {
    const id = String(webContentsId)
    const previous = reloads.get(id) || 0
    reloads.set(id, previous + 1)
    return { ...snapshot(), bridgeAttempts: reloads.get(id) }
  }

  function onStatusChange(listener) {
    listeners.add(listener)
    return () => listeners.delete(listener)
  }

  return {
    snapshot,
    markServer,
    markOAuthListener,
    awaitReady,
    queueDeepLink,
    consumeInitialDeepLinks,
    noteBridgeReady,
    onStatusChange,
  }
}

module.exports = { createShellBridge }
