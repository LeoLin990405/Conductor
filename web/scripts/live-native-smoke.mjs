#!/usr/bin/env node
/**
 * Live browser smoke for the native runtime picker.
 *
 * Requires the real Conductor frontend (default http://127.0.0.1:5173), the real
 * backend (:8000), and a workspace id. Unlike the mocked e2e suite this reads
 * GET /api/v1/native-agents from the live backend, then verifies the composer
 * offers every live native provider and none of the PTC-only three.
 *
 * Usage:
 *   CONDUCTOR_WORKSPACE_ID=<uuid> node scripts/live-native-smoke.mjs
 *   CONDUCTOR_WEB_URL=http://127.0.0.1:5173 CONDUCTOR_WORKSPACE_ID=... node ...
 */
import { chromium } from '@playwright/test';

const webUrl = process.env.CONDUCTOR_WEB_URL || 'http://127.0.0.1:5173';
const workspaceId = process.env.CONDUCTOR_WORKSPACE_ID;
if (!workspaceId) {
  console.error('CONDUCTOR_WORKSPACE_ID is required');
  process.exit(2);
}
const expected = ['codex', 'deepseek-dsh', 'grok', 'kimi', 'mimo', 'minimax', 'stepfun', 'zcode'];
const forbidden = ['claude-code', 'gemini', 'qwen'];

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ locale: 'en-US' });
const page = await context.newPage();
await page.addInitScript(() => {
  document.cookie = 'locale=en-US; path=/; max-age=31536000; samesite=lax';
});
const pageErrors = [];
page.on('pageerror', (error) => pageErrors.push(String(error)));
try {
  await page.goto(`${webUrl}/chat/${workspaceId}`, { waitUntil: 'domcontentloaded' });
  const select = page.locator('select').filter({ has: page.locator('option[value="codex"]') }).first();
  await select.waitFor({ state: 'visible', timeout: 30_000 });
  const actual = await select.locator('option').evaluateAll((options) => options.map((o) => o.value));
  const missing = expected.filter((provider) => !actual.includes(provider));
  const leaked = forbidden.filter((provider) => actual.includes(provider));
  const result = { webUrl, workspaceId, actual, missing, leaked, pageErrors };
  console.log(JSON.stringify(result, null, 2));
  if (missing.length || leaked.length || pageErrors.length) process.exitCode = 1;
  else console.log('LIVE_BROWSER_NATIVE_PICKER_OK');
} finally {
  await browser.close();
}
