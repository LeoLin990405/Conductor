'use strict'

/**
 * What the shell does NOT open or allow. Every case is a launch the audit found
 * going the other way: a loopback debugging port on every start, and a window
 * that could host a `<webview>` with no handler constraining it. Both gave any
 * local process (or any other OS user on the machine) the signed-in window.
 *
 * The behavioural cases drive the real modules over the electron stub. The
 * literal search at the end is the second line, for the code a stub launch does
 * not reach: the `ready` handler, which the stub never resolves.
 */

const { test, describe, after } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { EventEmitter } = require('node:events')
const { loadShell, cleanup, commandLineSwitches, emitApp, windows } = require('./helpers')

after(cleanup)

const ROOT = path.join(__dirname, '..')

describe('no debugging port', () => {
  const saved = process.env.CONDUCTOR_DESKTOP_CDP_PORT
  after(() => {
    if (saved === undefined) delete process.env.CONDUCTOR_DESKTOP_CDP_PORT
    else process.env.CONDUCTOR_DESKTOP_CDP_PORT = saved
  })

  for (const edition of ['oss', 'saas', 'core']) {
    test(`a ${edition} launch appends no command-line switch at all`, () => {
      delete process.env.CONDUCTOR_DESKTOP_CDP_PORT
      loadShell({ edition })
      // Not only `remote-debugging-*`: a switch changes what the whole process
      // exposes, so adding any should be a decision that lands in this file.
      assert.deepEqual(commandLineSwitches, [])
    })
  }

  test('the knob that used to choose the port no longer does anything', () => {
    process.env.CONDUCTOR_DESKTOP_CDP_PORT = '9333'
    loadShell({ edition: 'saas' })
    assert.deepEqual(commandLineSwitches, [])
  })
})

describe('no webview', () => {
  const SERVER = 'http://localhost:5173'

  // Every window the shell can make, across the editions: the app, the console's
  // own window and the server picker.
  const LAUNCHES = [
    ['saas, console in a window of its own', { edition: 'saas' }, (s) => s.store.set('platformChrome', false)],
    ['saas, signed in', { edition: 'saas' }, (s) => s.store.set('reachedApp', true)],
    ['oss, no server yet', { edition: 'oss' }],
    ['oss, server stored', { edition: 'oss', serverUrl: SERVER }],
    ['core, no server yet', { edition: 'core' }],
    ['core, server stored', { edition: 'core', serverUrl: SERVER }],
  ]

  for (const [name, options, prepare] of LAUNCHES) {
    test(`${name}: no window can host a <webview> or loosens its isolation`, () => {
      const shell = loadShell(options)
      if (prepare) prepare(shell)
      shell.main.openLaunchWindows()
      assert.ok(windows.length >= 1, 'the launch opened no window')
      for (const win of windows) {
        const prefs = win.options.webPreferences || {}
        const who = `'${win.options.title || 'main window'}'`
        assert.notEqual(prefs.webviewTag, true, `${who} can host a <webview>`)
        assert.equal(prefs.nodeIntegration, false, `${who} runs Node in the page`)
        assert.equal(prefs.contextIsolation, true, `${who} shares a world with the page`)
      }
    })
  }

  // The stub never resolves `whenReady`, so what registers on `app` at require
  // time is what a window created later is covered by.
  test('a webview that is attached anyway is refused', () => {
    loadShell({ edition: 'saas' })
    const contents = Object.assign(new EventEmitter(), {
      session: {
        setPermissionRequestHandler() {},
        setPermissionCheckHandler() {},
        on() {},
      },
    })
    emitApp('web-contents-created', {}, contents)

    let prevented = false
    contents.emit('will-attach-webview', { preventDefault() { prevented = true } }, {}, {})
    assert.equal(prevented, true, 'the attach was allowed to proceed')
  })
})

// The ready handler is the one block this suite cannot run, so the source is
// searched for the three spellings that would bring the feature back. A
// literal search of the files this package ships, not a parser: it can only
// fail closed, and a legitimate future use has to rewrite this test to say so.
describe('nothing in the shipped files brings them back', () => {
  const shipped = []
  for (const dir of ['src', 'setup', 'outage', 'scripts']) {
    for (const entry of fs.readdirSync(path.join(ROOT, dir))) {
      shipped.push(path.join(dir, entry))
    }
  }

  for (const [what, needle] of [
    ['a remote-debugging switch', 'remote-debugging'],
    ['a command-line switch of any kind', 'appendSwitch'],
    ['the webview tag', 'webviewTag'],
    ['the endpoint file the backend used to read', 'desktop-browser'],
  ]) {
    test(`no shipped file spells ${what}`, () => {
      assert.ok(shipped.length >= 20, `only found ${shipped.length} shipped files to search`)
      const hits = shipped.filter((file) => fs.readFileSync(path.join(ROOT, file), 'utf8').includes(needle))
      assert.deepEqual(hits, [], `${needle} appears in ${hits}`)
    })
  }
})
