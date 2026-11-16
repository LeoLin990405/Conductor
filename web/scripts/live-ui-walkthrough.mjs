#!/usr/bin/env node
/**
 * Real UI task walkthrough for the ink-and-paper shell:
 *   Research: submit a real question, land on a thread, receive an exact reply.
 *   Command Palette: search + navigate to Markets and Settings.
 *
 *   CONDUCTOR_WORKSPACE_ID is not required; ResearchHome resolves Home itself.
 *   CONDUCTOR_WEB_URL=http://127.0.0.1:5173 node scripts/live-ui-walkthrough.mjs
 */
import { chromium } from '@playwright/test';

const webUrl = process.env.CONDUCTOR_WEB_URL || 'http://127.0.0.1:5173';
const expected = process.env.UI_WALKTHROUGH_REPLY || 'UI-WALKTHROUGH-OK';
const prompt = process.env.UI_WALKTHROUGH_PROMPT
  || `请直接回复：${expected}。不要调用任何工具。`;

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ locale: 'en-US', viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
await page.addInitScript(() => { document.cookie = 'locale=en-US; path=/; max-age=31536000; samesite=lax'; });
const errors = [];
page.on('pageerror', (error) => errors.push(String(error)));
let failed = false;
try {
  // 1. Research task: real question -> real thread -> real assistant reply.
  await page.goto(`${webUrl}/research`, { waitUntil: 'domcontentloaded' });
  const researchBox = page.getByRole('textbox', { name: '研究问题' });
  await researchBox.waitFor({ state: 'visible', timeout: 30_000 });
  await researchBox.fill(prompt);
  await page.getByRole('button', { name: '开始研究' }).click();
  await page.waitForURL(/\/chat\/t\//, { timeout: 60_000 });
  await page.getByText(expected, { exact: true }).first().waitFor({ state: 'visible', timeout: 240_000 });
  const researchBody = await page.locator('body').innerText();
  if (/Max retries exceeded|SandboxTransientError|Something went wrong/.test(researchBody)) {
    throw new Error('Research replied, but the turn also ended with a sandbox/run error');
  }
  console.log('RESEARCH_TASK_OK', page.url());

  // 2. Command Palette: search and go to Markets.
  await page.keyboard.press('Meta+k');
  const palette = page.getByRole('dialog');
  await palette.waitFor({ state: 'visible', timeout: 10_000 });
  const search = palette.getByPlaceholder(/Search research, pages or commands/);
  await search.fill('markets');
  await page.keyboard.press('Enter');
  await page.waitForURL(/\/markets$/, { timeout: 20_000 });
  await page.getByRole('heading', { name: 'Market Overview' }).waitFor({ state: 'visible', timeout: 30_000 });
  const marketBody = await page.locator('body').innerText();
  if (!marketBody.includes('MARKETS · OVERVIEW') && !marketBody.includes('Market Overview')) {
    throw new Error('Markets did not render its overview');
  }
  // CN upstreams can be transiently unavailable; structure is the gate, live
  // index names are evidence when the source answers.
  const liveIndices = ['上证指数', '深证成指', '创业板指', '恒生指数'].filter((name) => marketBody.includes(name));
  console.log('PALETTE_MARKETS_OK', page.url(), `liveIndices=${liveIndices.length}`);

  // 3. Command Palette: search and go to Settings.
  await page.keyboard.press('Meta+k');
  await palette.waitFor({ state: 'visible', timeout: 10_000 });
  await palette.getByPlaceholder(/Search research, pages or commands/).fill('settings');
  await page.keyboard.press('Enter');
  await page.waitForURL(/\/settings/, { timeout: 20_000 });
  await page.getByRole('heading', { name: 'Settings', exact: true }).waitFor({ state: 'visible', timeout: 30_000 });
  console.log('PALETTE_SETTINGS_OK', page.url());

  // 4. Command Palette action: toggle the theme and restore it.
  const theme = () => page.evaluate(() => document.documentElement.dataset.theme);
  const beforeTheme = await theme();
  await page.keyboard.press('Meta+k');
  await palette.waitFor({ state: 'visible', timeout: 10_000 });
  await palette.getByPlaceholder(/Search research, pages or commands/).fill('toggle theme');
  await palette.getByRole('option', { name: /Toggle theme/ }).click();
  await page.waitForFunction((previous) => document.documentElement.dataset.theme !== previous, beforeTheme, { timeout: 10_000 });
  const afterTheme = await theme();

  await page.keyboard.press('Meta+k');
  await palette.waitFor({ state: 'visible', timeout: 10_000 });
  await palette.getByPlaceholder(/Search research, pages or commands/).fill('toggle theme');
  await palette.getByRole('option', { name: /Toggle theme/ }).click();
  await page.waitForFunction((previous) => document.documentElement.dataset.theme === previous, beforeTheme, { timeout: 10_000 });
  console.log('PALETTE_TOGGLE_THEME_OK', `${beforeTheme} -> ${afterTheme} -> ${await theme()}`);
} catch (error) {
  failed = true;
  console.error('UI_WALKTHROUGH_FAILED', String(error));
  try {
    const body = await page.locator('body').innerText();
    console.error('BODY_TAIL', body.slice(-2000));
  } catch { /* page may be gone */ }
} finally {
  console.log('PAGE_ERRORS', errors.length, errors.slice(0, 3));
  await browser.close();
}
if (failed || errors.length) process.exit(1);
