'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')

const { startShell } = require('../src/shell-startup')

test('the first window opens before a slow OAuth listener resolves when deferOAuth is on', async () => {
  let resolveOAuth
  const oauth = new Promise((resolve) => { resolveOAuth = resolve })
  const order = []
  const times = [10, 20, 30, 40]
  const { marks, oauthReady } = await startShell({
    startOAuth: () => { order.push('oauth'); return oauth },
    registerIpc: () => order.push('ipc'),
    installMenu: () => order.push('menu'),
    openWindows: () => order.push('windows'),
    deferOAuth: true,
    now: () => times.shift() || 50,
  })

  assert.deepEqual(order, ['oauth', 'ipc', 'menu', 'windows'])
  assert.deepEqual(marks, { ready: 10, ipc: 20, menu: 30, windows: 40 })
  assert.ok(oauthReady instanceof Promise)

  resolveOAuth('port')
  assert.equal(await oauthReady, 'port')
})

test('the default order still waits for the listener before opening windows', async () => {
  const order = []
  const { marks } = await startShell({
    startOAuth: async () => { order.push('oauth'); return 'port' },
    registerIpc: () => order.push('ipc'),
    installMenu: () => order.push('menu'),
    openWindows: () => order.push('windows'),
  })
  assert.deepEqual(order, ['oauth', 'ipc', 'menu', 'windows'])
  assert.ok(marks.windows >= marks.ready)
})

test('a rejected OAuth start still opens the window and reports the error', async () => {
  const errors = []
  const opened = []
  const { oauthReady } = await startShell({
    startOAuth: () => Promise.reject(new Error('no loopback')),
    registerIpc: () => {},
    installMenu: () => {},
    openWindows: () => opened.push('windows'),
    deferOAuth: true,
    onError: (err) => errors.push(String(err)),
  })

  assert.deepEqual(opened, ['windows'])
  assert.equal(await oauthReady, null)
  assert.deepEqual(errors, ['Error: no loopback'])
})

test('a synchronous OAuth throw is caught and does not block startup', async () => {
  const errors = []
  const opened = []
  const { oauthReady } = await startShell({
    startOAuth: () => { throw new Error('bind exploded') },
    registerIpc: () => opened.push('ipc'),
    installMenu: () => opened.push('menu'),
    openWindows: () => opened.push('windows'),
    onError: (err) => errors.push(String(err)),
  })

  assert.deepEqual(opened, ['ipc', 'menu', 'windows'])
  assert.equal(await oauthReady, null)
  assert.deepEqual(errors, ['Error: bind exploded'])
})
