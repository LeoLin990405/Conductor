'use strict'

/**
 * Ad-hoc sign the macOS bundle when no Developer ID is configured.
 *
 * electron-builder's `identity: null` skips signing entirely, which leaves the
 * Electron executable's linker signature without the bundle's CodeResources.
 * The app can still launch locally, but `codesign --verify` calls it invalid
 * and Gatekeeper cannot assess it. An ad-hoc signature makes the bundle
 * self-consistent for local/OSS installs; a real Developer ID build skips this
 * and lets electron-builder sign normally.
 */
const { execFileSync } = require('node:child_process')
const path = require('node:path')

module.exports = async function afterPack(context) {
  // The packager's target platform, not the host's: a Windows build made on
  // macOS has no .app to sign.
  if (context.electronPlatformName !== 'darwin') return
  if ((process.env.CSC_LINK || '').trim() || (process.env.CSC_NAME || '').trim()) {
    console.log('[after-pack] Developer ID signing configured; skipping ad-hoc signature')
    return
  }
  const app = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`)
  execFileSync('codesign', ['--force', '--deep', '--sign', '-', app], { stdio: 'inherit' })
  console.log(`[after-pack] ad-hoc signed ${app}`)
}
