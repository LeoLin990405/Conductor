#!/usr/bin/env node
/**
 * Live native HITL smoke: a real Codex turn requests command approval, the
 * browser approves the card, the command runs, and the turn resumes.
 *
 *   CONDUCTOR_WORKSPACE_ID=<uuid> node scripts/live-native-hitl-smoke.mjs
 *   HITL_PROVIDER=codex CONDUCTOR_WEB_URL=http://127.0.0.1:5173 ...
 */
import { existsSync, readFileSync, unlinkSync } from 'node:fs';
import { chromium } from '@playwright/test';

const workspaceId = process.env.CONDUCTOR_WORKSPACE_ID;
if (!workspaceId) {
  console.error('CONDUCTOR_WORKSPACE_ID is required');
  process.exit(2);
}
const webUrl = process.env.CONDUCTOR_WEB_URL || 'http://127.0.0.1:5173';
const provider = process.env.HITL_PROVIDER || 'codex';
const token = `HITL-${Date.now()}`;
const file = `/tmp/conductor-${provider}-hitl-${Date.now()}.txt`;
const expected = `${token}-OK`;

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ locale: 'en-US', viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
await page.addInitScript(() => { document.cookie = 'locale=en-US; path=/; max-age=31536000; samesite=lax'; });
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
let failed = false;
try {
  await page.goto(`${webUrl}/chat/${workspaceId}`, { waitUntil: 'domcontentloaded' });
  const select = page.locator('select').filter({ has: page.locator('option[value="codex"]') }).first();
  await select.waitFor({ state: 'visible', timeout: 30000 });
  await select.selectOption(provider);

  const composer = page.locator('textarea').first();
  await composer.waitFor({ state: 'visible', timeout: 15000 });
  await composer.fill(
    `Use your shell tool to create the file ${file} containing exactly OK. ` +
    `You must request my approval before running the command. ` +
    `After it runs, reply exactly: ${expected}`,
  );
  await page.locator('button[aria-label="Send message"]').first().click();

  const card = page.locator('[data-testid="tool-approval-card"]').first();
  await card.waitFor({ state: 'visible', timeout: 180000 });
  const cardText = await card.innerText();
  console.log('APPROVAL_CARD', cardText.replace(/\n+/g, ' | ').slice(0, 400));
  await card.getByRole('button', { name: /approve/i }).first().click();

  await page.getByText(expected, { exact: true }).first().waitFor({ state: 'visible', timeout: 180000 });
  if (!existsSync(file)) throw new Error(`approval resumed but ${file} was not created`);
  const content = readFileSync(file, 'utf8').trim();
  if (content !== 'OK') throw new Error(`unexpected file content: ${JSON.stringify(content)}`);
  console.log('NATIVE_HITL_OK', provider, page.url(), 'file=', file);
} catch (error) {
  failed = true;
  console.error('NATIVE_HITL_FAILED', String(error));
  try {
    const body = await page.locator('body').innerText();
    console.error('BODY_TAIL', body.slice(-2000));
  } catch { /* page may be gone */ }
} finally {
  if (existsSync(file)) {
    try { unlinkSync(file); } catch { /* best effort */ }
  }
  console.log('PAGE_ERRORS', errors.length, errors.slice(0, 3));
  await browser.close();
}
if (failed || errors.length) process.exit(1);
