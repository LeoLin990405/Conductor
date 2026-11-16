'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')

const { createShellBridge } = require('../src/shell-bridge')

test('readiness starts unconfigured and resolves with the adopted origin', async () => {
  const bridge = createShellBridge({ now: () => 1 })
  assert.equal(bridge.snapshot().state, 'starting')
  const pending = bridge.awaitReady()
  bridge.markServer('https://app.example.com')
  const value = await pending
  assert.equal(value.state, 'ready')
  assert.equal(value.serverUrl, 'https://app.example.com')
})

test('readiness resolves immediately once a server is configured', async () => {
  const bridge = createShellBridge()
  bridge.markServer('https://app.example.com')
  const value = await bridge.awaitReady()
  assert.equal(value.serverUrl, 'https://app.example.com')
})

test('an OAuth listener failure is visible and settles waiters with an error', async () => {
  const bridge = createShellBridge()
  const pending = bridge.awaitReady()
  bridge.markOAuthListener('failed', 'bind refused')
  const value = await pending
  assert.equal(value.state, 'starting')
  assert.equal(value.oauthListener, 'failed')
  assert.match(value.error, /bind refused/)
})

test('initial deep links are consumed exactly once', () => {
  const bridge = createShellBridge()
  bridge.queueDeepLink('conductor://callback?code=A')
  bridge.queueDeepLink('conductor://callback?code=B')
  assert.deepEqual(bridge.consumeInitialDeepLinks(), [
    'conductor://callback?code=A',
    'conductor://callback?code=B',
  ])
  assert.deepEqual(bridge.consumeInitialDeepLinks(), [])
})

test('bridge handshakes count reloads per webContents without duplicating state', () => {
  const bridge = createShellBridge()
  assert.equal(bridge.noteBridgeReady(7).bridgeAttempts, 1)
  assert.equal(bridge.noteBridgeReady(7).bridgeAttempts, 2)
  assert.equal(bridge.noteBridgeReady(8).bridgeAttempts, 1)
  assert.deepEqual(bridge.snapshot().reloads, { 7: 2, 8: 1 })
})

test('status listeners see readiness and listener state changes', () => {
  const bridge = createShellBridge()
  const seen = []
  bridge.onStatusChange((value) => seen.push(value.oauthListener + ':' + value.state))
  bridge.markServer('https://app.example.com')
  bridge.markOAuthListener('ready')
  assert.deepEqual(seen, ['pending:ready', 'ready:ready'])
})
