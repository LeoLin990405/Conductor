#!/usr/bin/env node
/**
 * Live browser native-turn smoke against the real frontend/backend.
 *
 * Usage:
 *   CONDUCTOR_WORKSPACE_ID=<uuid> PROVIDERS=grok node scripts/live-native-turn-smoke.mjs
 *   CONDUCTOR_WEB_URL=http://127.0.0.1:5173 CONDUCTOR_WORKSPACE_ID=...  *     PROVIDERS=kimi,stepfun node scripts/live-native-turn-smoke.mjs
 */
import { chromium } from '@playwright/test';

const workspaceId = process.env.CONDUCTOR_WORKSPACE_ID;
if (!workspaceId) {
  console.error('CONDUCTOR_WORKSPACE_ID is required');
  process.exit(2);
}
const webUrl = process.env.CONDUCTOR_WEB_URL || 'http://127.0.0.1:5173';
const providers = process.env.PROVIDERS ? process.env.PROVIDERS.split(',') : ['grok'];
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ locale: 'en-US' });
const page = await context.newPage();
await page.addInitScript(() => { document.cookie = 'locale=en-US; path=/; max-age=31536000; samesite=lax'; });
const errors = [];
page.on('pageerror', e => errors.push(String(e)));
for (const provider of providers) {
  const expected = `browser-${provider}-ok`;
  await page.goto(`${webUrl}/chat/${workspaceId}`, { waitUntil: 'domcontentloaded' });
  const select = page.locator('select').filter({ has: page.locator('option[value="codex"]') }).first();
  await select.waitFor({ state: 'visible', timeout: 30000 });
  await select.selectOption(provider);
  const composer = page.locator('textarea').first();
  await composer.waitFor({ state: 'visible', timeout: 15000 });
  await composer.fill(`Reply with exactly: ${expected}`);
  const send = page.locator('button[aria-label="Send message"]').first();
  await send.click();
  await page.waitForURL(/\/chat\/t\//, { timeout: 30000 });
  await page.getByText(expected, { exact: true }).first().waitFor({ state: 'visible', timeout: 180000 });
  console.log('PROVIDER_TURN_OK', provider, page.url());
}
console.log('PAGE_ERRORS', errors.length, errors.slice(0, 3));
await browser.close();
if (errors.length) process.exit(1);
