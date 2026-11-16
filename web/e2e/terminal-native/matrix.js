/**
 * Shared screenshot matrix for terminal-native web slices.
 *
 * Every page slice imports `shootMatrix` and leaves the PNG tree under
 * `web/test-results/terminal-native/<slice>/` for a reviewer; `/test-results/`
 * is already gitignored.
 *
 * Theme is `localStorage.theme` (read by index.html before paint); locale is
 * the `locale` cookie (read by resolveLocale() before i18n boots).
 *
 * Why each cell opens its own page: e2e/fixtures.js gives the test `page` an
 * init script that writes `locale=en-US` into document.cookie on every
 * navigation, before any app script runs. Overriding that from a second init
 * script would depend on registration order, and switching after paint would
 * screenshot a document that booted in English. Init scripts added with
 * page.addInitScript belong to that one page, so a sibling page from the same
 * context carries none of them, while it still shares the context-level API
 * redirect fixtures.js installs and the context cookie jar. On that page the
 * cookie set with context.addCookies right before navigation is the only
 * locale the document ever sees. The rendered result is then asserted
 * (cookie, <html lang>, CJK text present or absent), so a cell that rendered
 * the wrong language fails instead of writing a misleading PNG.
 */
import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, test } from '@playwright/test';

import { mockAPI } from '../fixtures.js';

export const THEMES = ['dark', 'light'];
export const WIDTHS = [1440, 390];
export const LOCALES = ['en-US', 'zh-CN'];
const HEIGHT_FOR = { 1440: 900, 390: 844 };

// Any CJK Unified Ideograph. zh-CN copy always has some; en-US copy has none.
const CJK = /[一-鿿]/;

const RESULTS_ROOT = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  '../../test-results/terminal-native',
);

/**
 * Wait until the authenticated shell has painted a real route.
 *
 * Absence of `.page-loading` alone also matches an empty `#root`, so require
 * `main.app-main` under `.app-layout` with painted route content. Timeouts
 * propagate — a loading screenshot must not pass.
 *
 * @param {import('@playwright/test').Page} page
 * @param {{ timeout?: number }} [opts]
 */
export async function waitForRenderedRoute(page, { timeout = 15_000 } = {}) {
  await page.waitForFunction(
    () => {
      if (document.querySelector('.page-loading')) return false;
      const layout = document.querySelector('.app-layout');
      if (!layout) return false;
      const main = layout.querySelector('main.app-main');
      if (!main) return false;
      const painted = main.querySelector('.route-fade-in, .sa-page, .settings-page, .main');
      if (!painted) return false;
      return (painted.textContent || '').trim().length > 0;
    },
    null,
    { timeout },
  );
}

/**
 * Open `routePath` on a fresh page of `page`'s context with the given theme,
 * viewport and locale in effect from the first byte of the document.
 *
 * The returned page has no fixtures.js init script, so nothing rewrites the
 * locale cookie. The caller closes it.
 *
 * @param {import('@playwright/test').Page} page any page of the test's context
 * @param {{ path: string, theme?: string, width?: number, locale?: string,
 *           reducedMotion?: 'reduce' | 'no-preference',
 *           mockOverrides?: Record<string, unknown> }} opts
 *   `mockOverrides` is passed to fixtures.js `mockAPI` for the fresh page.
 *   Routes a spec registered on its own `page` do not reach this page (page
 *   routes are per page), so overrides must come through here.
 * @returns {Promise<import('@playwright/test').Page>}
 */
export async function openRoute(
  page,
  {
    path: routePath,
    theme = 'dark',
    width = 1440,
    locale = 'en-US',
    reducedMotion,
    mockOverrides,
  },
) {
  const context = page.context();
  const base = page.url().startsWith('http') ? page.url() : test.info().project.use.baseURL;
  const baseURL = new URL(routePath, base).origin;

  const cell = await context.newPage();
  await cell.setViewportSize({ width, height: HEIGHT_FOR[width] ?? 900 });
  if (reducedMotion) await cell.emulateMedia({ reducedMotion });
  await mockAPI(cell, mockOverrides ?? {});
  // Theme rides localStorage, which has no other writer, so an init script is
  // order-safe here. Every navigation of this page starts in the wanted theme.
  await cell.addInitScript((t) => {
    try {
      localStorage.setItem('theme', t);
    } catch {
      /* storage blocked: index.html falls back, and the assert below catches it */
    }
  }, theme);
  // The cookie in the jar at navigation time is the one the document reads.
  // Set it last so no earlier write of this context can be the one in effect.
  await context.addCookies([
    { name: 'locale', value: locale, url: baseURL, sameSite: 'Lax' },
  ]);
  await cell.goto(routePath, { waitUntil: 'domcontentloaded' });
  await waitForRenderedRoute(cell);
  await cell.waitForFunction((loc) => document.documentElement.lang === loc, locale, {
    timeout: 15_000,
  });
  return cell;
}

/**
 * Assert the document rendered in `locale`: cookie, <html lang>, and the
 * visible UI text (CJK present for zh-CN, absent for en-US), plus the shell
 * nav label in the expected language.
 *
 * @param {import('@playwright/test').Page} page
 * @param {string} locale
 * @param {string} label for failure messages
 */
export async function assertRenderedLocale(page, locale, label = '') {
  const state = await page.evaluate(() => {
    // Visible UI copy only. A language picker names each language in its own
    // script (an <option> or an element with its own lang), which is not copy
    // in the page's language and must not count either way.
    const uiText = (root) => {
      if (!root) return '';
      const out = [];
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        const el = n.parentElement;
        if (!el || el.closest('script, style, select, option, template')) continue;
        const own = el.closest('[lang]');
        if (own && own !== document.documentElement) continue;
        if (!el.getClientRects().length) continue;
        out.push(n.textContent);
      }
      // Accessible names count too: an icon-only control (the mobile menu
      // button) carries its copy in an attribute, not a text node.
      for (const el of root.querySelectorAll('[aria-label], [title], [placeholder], img[alt]')) {
        if (el.closest('select, option, template')) continue;
        const own = el.closest('[lang]');
        if (own && own !== document.documentElement) continue;
        if (!el.getClientRects().length) continue;
        for (const a of ['aria-label', 'title', 'placeholder', 'alt']) {
          const v = el.getAttribute(a);
          if (v) out.push(`[${a}=${v}]`);
        }
      }
      return out.join(' ');
    };
    const m = document.cookie.match(/(?:^|;\s*)locale=([^;]+)/);
    return {
      cookie: m ? decodeURIComponent(m[1]) : '',
      lang: document.documentElement.lang,
      shell: uiText(document.body),
    };
  });
  expect(state.cookie, `${label} locale cookie`).toBe(locale);
  if (locale === 'zh-CN') {
    expect(state.lang, `${label} html lang`).toMatch(/^zh/);
    // The rendered app UI (shell + route) must carry Chinese copy. Not the route
    // container alone: InboxPage hardcodes its English copy today, so on
    // /inbox only the shell is translated.
    expect(
      CJK.test(state.shell),
      `${label} rendered UI has CJK: ${state.shell.replace(/\s+/g, ' ').slice(0, 300)}`,
    ).toBe(true);
    // The shell nav follows the catalog; its English label must be gone.
    await expect(page.getByRole('button', { name: '收件箱' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Inbox', exact: true })).toHaveCount(0);
  } else {
    expect(state.lang, `${label} html lang`).toMatch(/^en/);
    expect(
      CJK.test(state.shell),
      `${label} rendered UI has no CJK: ${state.shell.match(/[^\s]*[\u4e00-\u9fff][^\s]*/g)}`,
    ).toBe(false);
    await expect(page.getByRole('button', { name: 'Inbox', exact: true })).toBeVisible();
  }
}

/**
 * Assert the document stamped the requested theme.
 *
 * @param {import('@playwright/test').Page} page
 * @param {string} theme
 * @param {string} label
 */
export async function assertRenderedTheme(page, theme, label = '') {
  const got = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
  expect(got, `${label} data-theme`).toBe(theme);
}

/**
 * Walk {dark,light} × {1440,390} × {en-US,zh-CN} for one route: each cell
 * opens a fresh page with theme, width and locale in effect before the
 * document loads, asserts the rendered theme and language and that neither
 * the page nor main.app-main scrolls sideways, and writes
 * `<slice>/<name>-<theme>-<width>-<locale>.png`.
 *
 * @param {import('@playwright/test').Page} page
 * Options beyond the S01 contract, both optional and backwards-compatible:
 *  - `prepare(cellPage)` runs in each cell after the route has rendered and
 *    the theme and locale are asserted, before the overflow checks and the
 *    screenshot (open the palette, fill a query, …). It receives the cell's
 *    own page, not the `page` passed in.
 *  - `mockOverrides` goes to `mockAPI` for every cell page (see openRoute).
 *
 * Each cell is a fresh page carrying exactly one init script (theme), so init
 * scripts never stack across cells; that is why this design needs no
 * once-per-page guard. The page is closed after its shot.
 *
 * @param {{ slice: string, name: string, path: string,
 *           prepare?: (page: import('@playwright/test').Page) => Promise<void>,
 *           mockOverrides?: Record<string, unknown> }} opts
 * @returns {Promise<string[]>} the PNG paths written
 */
export async function shootMatrix(
  page,
  { slice, name, path: routePath, prepare, mockOverrides },
) {
  const outDir = path.join(RESULTS_ROOT, slice);
  await mkdir(outDir, { recursive: true });
  const written = [];

  for (const theme of THEMES) {
    for (const width of WIDTHS) {
      for (const locale of LOCALES) {
        const label = `${routePath} @ ${theme}/${width}/${locale}:`;
        const cell = await openRoute(page, {
          path: routePath,
          theme,
          width,
          locale,
          mockOverrides,
        });
        try {
          await assertRenderedTheme(cell, theme, label);
          await assertRenderedLocale(cell, locale, label);
          if (prepare) await prepare(cell);

          const scroll = await cell.evaluate(() => ({
            scrollWidth: document.scrollingElement.scrollWidth,
            clientWidth: document.scrollingElement.clientWidth,
          }));
          expect(
            scroll.scrollWidth,
            `${label} scrolls horizontally (${JSON.stringify(scroll)})`,
          ).toBeLessThanOrEqual(scroll.clientWidth);

          // Under 768px the app pins <body> with `position: fixed`, so the
          // document never scrolls and the check above cannot fail there.
          // The route scrolls inside main.app-main: hold that box to its width.
          const main = await cell.evaluate(() => {
            const el = document.querySelector('main.app-main');
            return el && { scrollWidth: el.scrollWidth, clientWidth: el.clientWidth };
          });
          expect(main, `${label} has no main.app-main`).toBeTruthy();
          expect(
            main.scrollWidth,
            `${label} main.app-main scrolls horizontally (${JSON.stringify(main)})`,
          ).toBeLessThanOrEqual(main.clientWidth);

          const file = path.join(outDir, `${name}-${theme}-${width}-${locale}.png`);
          await cell.screenshot({ path: file });
          written.push(file);
        } finally {
          await cell.close();
        }
      }
    }
  }
  return written;
}
