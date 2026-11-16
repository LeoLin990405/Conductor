/**
 * S28 acceptance for the terminal-native Legal page (/legal):
 *
 *  - the screenshot matrix {dark,light} × {1440,390} × {en-US,zh-CN};
 *  - Tab reaches every link and each wears the 2px ink.focus ring;
 *  - readable measure at 1440, full width (less gutters) at 390;
 *  - under reducedMotion 'reduce' no transition or animation duration is > 0.
 *
 * /legal is mounted in App.tsx outside AuthenticatedShell (a public page), so
 * matrix.js's openRoute/shootMatrix — which wait for `.app-layout
 * main.app-main` and assert the shell's Inbox nav label — cannot drive it.
 * This spec keeps the same per-cell contract (fresh sibling page, theme via
 * localStorage, locale via the cookie set right before navigation, rendered
 * theme + language asserted, no sideways scroll, PNG under
 * test-results/terminal-native/legal/) with a local opener that waits for the
 * Legal page itself. matrix.js stays import-only.
 */
import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, test, mockAPI } from '../fixtures.js';

import { LOCALES, THEMES, WIDTHS, assertRenderedTheme } from './matrix.js';

const ROUTE = '/legal';
const HEIGHT_FOR = { 1440: 900, 390: 844 };
const CJK = /[一-鿿]/;
const OUT_DIR = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  '../../test-results/terminal-native/legal',
);
const INK_FOCUS_RGB = { dark: 'rgb(139, 156, 255)', light: 'rgb(63, 79, 200)' };
const TITLE = { 'en-US': 'Legal & Attribution', 'zh-CN': '法律与署名' };

/** Open /legal on a fresh page of `page`'s context (see the header). */
async function openLegal(page, { theme = 'dark', width = 1440, locale = 'en-US', reducedMotion } = {}) {
  const context = page.context();
  const base = page.url().startsWith('http') ? page.url() : test.info().project.use.baseURL;
  const baseURL = new URL(ROUTE, base).origin;

  const cell = await context.newPage();
  await cell.setViewportSize({ width, height: HEIGHT_FOR[width] ?? 900 });
  if (reducedMotion) await cell.emulateMedia({ reducedMotion });
  await mockAPI(cell, {});
  await cell.addInitScript((t) => {
    try {
      localStorage.setItem('theme', t);
    } catch {
      /* storage blocked: the theme assertion catches it */
    }
  }, theme);
  await context.addCookies([{ name: 'locale', value: locale, url: baseURL, sameSite: 'Lax' }]);
  await cell.goto(ROUTE, { waitUntil: 'domcontentloaded' });
  await expect(cell.getByTestId('legal-page')).toBeVisible({ timeout: 15_000 });
  await cell.waitForFunction((loc) => document.documentElement.lang === loc, locale, {
    timeout: 15_000,
  });
  return cell;
}

/** Cookie, <html lang>, the h1 in the right language, CJK present iff zh-CN. */
async function assertLegalLocale(cell, locale, label) {
  const state = await cell.evaluate(() => {
    const m = document.cookie.match(/(?:^|;\s*)locale=([^;]+)/);
    const root = document.querySelector('[data-testid="legal-page"]');
    return {
      cookie: m ? decodeURIComponent(m[1]) : '',
      lang: document.documentElement.lang,
      text: root ? root.innerText : '',
    };
  });
  expect(state.cookie, `${label} locale cookie`).toBe(locale);
  expect(state.lang, `${label} html lang`).toMatch(locale === 'zh-CN' ? /^zh/ : /^en/);
  expect(CJK.test(state.text), `${label} CJK in page copy`).toBe(locale === 'zh-CN');
  await expect(cell.getByRole('heading', { level: 1, name: TITLE[locale] })).toBeVisible();
}

// ── page-evaluate helpers (self-contained; run in the browser) ──

function tagLinks() {
  const root = document.querySelector('[data-testid="legal-page"]');
  if (!root) throw new Error('legal page is not on the page');
  const out = [];
  for (const el of root.querySelectorAll('a[href], button, [tabindex]')) {
    if (el.tabIndex < 0 || !el.getClientRects().length) continue;
    const i = out.length;
    el.setAttribute('data-tn-ctl', String(i));
    out.push({ i, desc: `${el.tagName.toLowerCase()} ${el.textContent.trim().slice(0, 40)}` });
  }
  return out;
}

function focusedRing() {
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) {
    return { ctl: null, body: true, seen: false };
  }
  const seen = el.hasAttribute('data-tn-seen');
  el.setAttribute('data-tn-seen', '');
  const cs = getComputedStyle(el);
  return {
    ctl: el.getAttribute('data-tn-ctl'),
    body: false,
    seen,
    style: cs.outlineStyle,
    width: cs.outlineWidth,
    color: cs.outlineColor,
    offset: cs.outlineOffset,
  };
}

function resolveInkFocus() {
  const probe = document.createElement('div');
  probe.style.outlineColor = 'var(--ink-focus)';
  document.body.append(probe);
  const color = getComputedStyle(probe).outlineColor;
  probe.remove();
  return color;
}

test.describe('terminal-native legal', () => {
  test('matrix: /legal in both themes, widths and locales', async ({ page }) => {
    test.setTimeout(240_000);
    await mkdir(OUT_DIR, { recursive: true });
    const written = [];
    for (const theme of THEMES) {
      for (const width of WIDTHS) {
        for (const locale of LOCALES) {
          const label = `${ROUTE} @ ${theme}/${width}/${locale}:`;
          const cell = await openLegal(page, { theme, width, locale });
          try {
            await assertRenderedTheme(cell, theme, label);
            await assertLegalLocale(cell, locale, label);

            const box = await cell.evaluate(() => {
              const se = document.scrollingElement;
              const root = document.querySelector('[data-testid="legal-page"]');
              const article = root.querySelector('article');
              const cs = getComputedStyle(root);
              const body = getComputedStyle(root.querySelector('.tn-legal-prose'));
              const h1 = getComputedStyle(root.querySelector('h1'));
              const probe = (v) => {
                const el = document.createElement('span');
                el.style.fontFamily = v;
                document.body.append(el);
                const f = getComputedStyle(el).fontFamily;
                el.remove();
                return f;
              };
              const bg = document.createElement('div');
              bg.style.backgroundColor = 'var(--surface-page)';
              document.body.append(bg);
              const surface = getComputedStyle(bg).backgroundColor;
              bg.remove();
              return {
                page: { scrollWidth: se.scrollWidth, clientWidth: se.clientWidth },
                root: { scrollWidth: root.scrollWidth, clientWidth: root.clientWidth },
                articleWidth: article.getBoundingClientRect().width,
                viewport: document.documentElement.clientWidth,
                bodyFont: body.fontFamily,
                proseFont: probe('var(--font-prose)'),
                headingFont: h1.fontFamily,
                monoFont: probe('var(--font-mono)'),
                background: cs.backgroundColor,
                surface,
              };
            });
            expect(box.page.scrollWidth, `${label} page scrolls sideways`).toBeLessThanOrEqual(
              box.page.clientWidth,
            );
            expect(box.root.scrollWidth, `${label} legal root scrolls sideways`).toBeLessThanOrEqual(
              box.root.clientWidth,
            );
            expect(box.bodyFont, `${label} body uses the prose face`).toBe(box.proseFont);
            expect(box.headingFont, `${label} headings use the mono face`).toBe(box.monoFont);
            expect(box.background, `${label} token page surface`).toBe(box.surface);
            if (width === 1440) {
              // Readable measure: the column stays well short of the viewport.
              expect(box.articleWidth, `${label} measure`).toBeLessThanOrEqual(760);
            } else {
              // Full width: only the two 16px gutters are left over.
              expect(box.articleWidth, `${label} full width`).toBeGreaterThanOrEqual(box.viewport - 40);
            }

            const file = path.join(OUT_DIR, `legal-${theme}-${width}-${locale}.png`);
            await cell.screenshot({ path: file });
            written.push(file);
          } finally {
            await cell.close();
          }
        }
      }
    }
    expect(written, 'legal: 8 PNGs').toHaveLength(8);
  });

  for (const theme of THEMES) {
    test(`${theme}: Tab reaches every link and each wears the 2px ink.focus ring`, async ({ page }) => {
      test.setTimeout(90_000);
      const cell = await openLegal(page, { theme, locale: 'en-US' });
      try {
        await assertRenderedTheme(cell, theme, ROUTE);
        const focusRgb = await cell.evaluate(resolveInkFocus);
        expect(focusRgb, `${ROUTE} ${theme}: --ink-focus`).toBe(INK_FOCUS_RGB[theme]);

        const controls = await cell.evaluate(tagLinks);
        // Back link + repository link at least (privacy rides VITE_PRIVACY_URL).
        expect(controls.length, `${ROUTE}: links found`).toBeGreaterThanOrEqual(2);

        await cell.evaluate(() => document.activeElement?.blur());
        const reached = new Map();
        for (let i = 0; i < 40; i += 1) {
          await cell.keyboard.press('Tab');
          const ring = await cell.evaluate(focusedRing);
          if (ring.seen || (ring.body && i > 0)) break;
          if (ring.ctl == null) continue;
          reached.set(ring.ctl, ring);
        }

        const unreached = controls.filter((c) => !reached.has(String(c.i))).map((c) => c.desc);
        expect(unreached, `${ROUTE} ${theme}: links Tab never reached`).toEqual([]);

        const bad = [];
        for (const c of controls) {
          const r = reached.get(String(c.i));
          const ok =
            r.style === 'solid' &&
            r.width === '2px' &&
            r.color === focusRgb &&
            (r.offset === '2px' || r.offset === '-2px');
          if (!ok) bad.push(`${c.desc}: outline ${r.style} ${r.width} ${r.color} offset ${r.offset}`);
        }
        expect(bad, `${ROUTE} ${theme}: links without the ink.focus ring`).toEqual([]);
      } finally {
        await cell.close();
      }
    });
  }

  test("reducedMotion 'reduce' leaves no transition or animation duration > 0", async ({ page }) => {
    const cell = await openLegal(page, { reducedMotion: 'reduce' });
    try {
      const offenders = await cell.evaluate(() => {
        const parseSeconds = (value) =>
          value.split(',').map((part) => {
            const raw = part.trim();
            if (!raw || raw === 'normal' || raw === 'auto') return 0;
            const n = Number.parseFloat(raw);
            if (!Number.isFinite(n)) return 0;
            return raw.endsWith('ms') ? n / 1000 : n;
          });
        const bad = [];
        for (const el of document.querySelectorAll('*')) {
          for (const pseudo of [null, '::before', '::after']) {
            const cs = getComputedStyle(el, pseudo);
            const durations = [
              ...parseSeconds(cs.transitionDuration),
              ...parseSeconds(cs.animationDuration),
            ];
            if (durations.some((d) => d > 0)) {
              bad.push(`${el.tagName.toLowerCase()}.${el.className}${pseudo ?? ''}`);
            }
          }
        }
        return bad.slice(0, 20);
      });
      expect(offenders, `${ROUTE}: durations > 0 under reduced motion`).toEqual([]);
    } finally {
      await cell.close();
    }
  });

  for (const theme of THEMES) {
    test(`${theme}: no border-radius above radius.card`, async ({ page }) => {
      const cell = await openLegal(page, { theme });
      try {
        const tooRound = await cell.evaluate(() => {
          const bad = [];
          for (const el of document.querySelectorAll('[data-testid="legal-page"], [data-testid="legal-page"] *')) {
            const v = parseFloat(getComputedStyle(el).borderTopLeftRadius);
            if (Number.isFinite(v) && v > 3.5) bad.push(`${el.tagName.toLowerCase()}.${el.className}`);
          }
          return bad;
        });
        expect(tooRound, `${ROUTE} ${theme}: radius`).toEqual([]);
      } finally {
        await cell.close();
      }
    });
  }
});
