#!/usr/bin/env node
/**
 * Live desktop/mobile smoke for the key routes. Uses the real frontend and
 * backend; fails on page errors, blank routes, or horizontal overflow.
 *
 *   CONDUCTOR_WORKSPACE_ID=<uuid> node scripts/live-page-smoke.mjs
 */
import { chromium } from '@playwright/test';

const webUrl = process.env.CONDUCTOR_WEB_URL || 'http://127.0.0.1:5173';
const workspaceId = process.env.CONDUCTOR_WORKSPACE_ID;
if (!workspaceId) {
  console.error('CONDUCTOR_WORKSPACE_ID is required');
  process.exit(2);
}
const routes = [
  '/research',
  '/markets',
  '/dashboard',
  '/chat',
  `/chat/${workspaceId}`,
  '/memory',
  '/automations',
  '/plugins',
  '/settings',
  '/settings?tab=experiments',
];
const viewports = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'mobile', width: 390, height: 844 },
];
const browser = await chromium.launch({ headless: true });
const failures = [];
for (const viewport of viewports) {
  const context = await browser.newContext({ locale: 'en-US', viewport });
  const page = await context.newPage();
  await page.addInitScript(() => {
    document.cookie = 'locale=en-US; path=/; max-age=31536000; samesite=lax';
  });
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(String(error)));
  for (const route of routes) {
    pageErrors.length = 0;
    await page.goto(`${webUrl}${route}`, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => !document.querySelector('.page-loading'), null, { timeout: 30_000 }).catch(() => {});
    await page.waitForTimeout(800);
    const metrics = await page.evaluate(() => ({
      textLength: (document.body.innerText || '').trim().length,
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      loading: !!document.querySelector('.page-loading'),
    }));
    if (pageErrors.length || metrics.textLength < 20 || metrics.loading || metrics.overflow > 2) {
      failures.push({ viewport: viewport.name, route, pageErrors, metrics });
    }
  }
  await context.close();
}
await browser.close();
console.log(JSON.stringify({ checked: routes.length * viewports.length, failures }, null, 2));
if (failures.length) process.exitCode = 1;
else console.log('LIVE_PAGE_SMOKE_OK');
