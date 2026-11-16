/**
 * S25 acceptance for the terminal-native Settings page.
 *
 *  - /settings is shot through the full theme × width × locale matrix on the
 *    local-settings, model (ConnectedAccounts) and preferences sections;
 *  - at 390px, in en-US and zh-CN, the page does not scroll sideways and every
 *    control sits inside the viewport, now that S01's temporary tokens.css
 *    override is gone and Settings.css owns the minmax(0, 1fr) track;
 *  - Tab reaches every primary control of each section and each wears the
 *    2px ink.focus ring;
 *  - under reducedMotion 'reduce' no element has a transition or animation
 *    duration above 0.
 *
 * "Primary controls" are enumerated from the DOM, not hand-picked: every
 * focusable, visible, enabled control inside `main.app-main`.
 */
import { expect, test } from '../fixtures.js';

import {
  LOCALES,
  THEMES,
  assertRenderedLocale,
  assertRenderedTheme,
  openRoute,
  shootMatrix,
} from './matrix.js';

const ROUTES = [
  { name: 'local', path: '/settings' },
  { name: 'model', path: '/settings?tab=model' },
  { name: 'preferences', path: '/settings?tab=preferences' },
];

/** tokens.json ink.focus per theme, as Chromium serializes it. */
const INK_FOCUS_RGB = {
  dark: 'rgb(139, 156, 255)', // #8B9CFF
  light: 'rgb(63, 79, 200)', // #3F4FC8
};

const OVERRIDES = {
  'GET /features': { features: [] },
  'GET /native-agents': { runtimes: [] },
};

/**
 * Tag every primary control in the route with `data-tn-ctl` and describe it.
 * Runs in the page.
 */
function tagPrimaryControls() {
  const SEL = [
    'a[href]',
    'button',
    'input:not([type="hidden"])',
    'select',
    'textarea',
    'summary',
    '[role="tab"]',
    '[role="button"]',
    '[role="link"]',
    '[role="switch"]',
    '[role="checkbox"]',
    '[role="radio"]',
    '[role="combobox"]',
    '[contenteditable="true"]',
    '[tabindex]',
  ].join(', ');
  const main = document.querySelector('main.app-main');
  if (!main) throw new Error('main.app-main is not on the page');

  const describe = (el) => {
    const id = el.id ? `#${el.id}` : '';
    const cls = el.classList.length ? `.${[...el.classList].slice(0, 3).join('.')}` : '';
    const role = el.getAttribute('role') ? `[role=${el.getAttribute('role')}]` : '';
    const name = (
      el.getAttribute('aria-label') ||
      el.textContent ||
      el.getAttribute('placeholder') ||
      el.getAttribute('name') ||
      ''
    )
      .trim()
      .replace(/\s+/g, ' ')
      .slice(0, 40);
    return `${el.tagName.toLowerCase()}${id}${cls}${role} "${name}"`;
  };

  const visible = (el) => {
    if (!el.getClientRects().length) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none';
  };

  const seenRadioGroups = new Set();
  const out = [];
  for (const el of main.querySelectorAll(SEL)) {
    if (el.tabIndex < 0) continue;
    if (el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true') continue;
    if (el.closest('[inert], [aria-hidden="true"]')) continue;
    if (!visible(el)) continue;
    if (el.matches('input[type="radio"]') && el.name) {
      const key = `${el.form ? 'f' : ''}:${el.name}`;
      if (seenRadioGroups.has(key)) continue;
      seenRadioGroups.add(key);
    }
    const i = out.length;
    el.setAttribute('data-tn-ctl', String(i));
    out.push({ i, desc: describe(el) });
  }
  return out;
}

/**
 * The ring the focused control wears, measured on whichever element draws it.
 * Runs in the page.
 */
function focusedRing() {
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) {
    return { ctl: null, body: true, seen: false };
  }
  const seen = el.hasAttribute('data-tn-seen');
  el.setAttribute('data-tn-seen', '');
  let bearer = el;
  const own = getComputedStyle(el);
  if (own.outlineStyle === 'none' && el.parentElement?.matches('.rings-within')) {
    bearer = el.parentElement;
  }
  const cs = getComputedStyle(bearer);
  return {
    ctl: el.getAttribute('data-tn-ctl'),
    body: false,
    seen,
    tag: el.tagName.toLowerCase(),
    bearer: bearer === el ? 'self' : '.rings-within',
    style: cs.outlineStyle,
    width: cs.outlineWidth,
    color: cs.outlineColor,
    offset: cs.outlineOffset,
    shadow: cs.boxShadow,
  };
}

/** Resolve `--ink-focus` from :root to a computed colour. Runs in the page. */
function resolveInkFocus() {
  const declared = getComputedStyle(document.documentElement)
    .getPropertyValue('--ink-focus')
    .trim();
  if (!declared) return null;
  const probe = document.createElement('div');
  probe.style.outlineColor = 'var(--ink-focus)';
  document.body.append(probe);
  const color = getComputedStyle(probe).outlineColor;
  probe.remove();
  return color;
}

/** Why a measured ring is not the 2px ink.focus ring, or '' when it is. */
function ringProblem(ring, focusRgb) {
  const transparent = (c) => c === 'transparent' || /,\s*0\)\s*$/.test(c);
  const outlineOk =
    ring.style === 'solid' &&
    ring.width === '2px' &&
    ring.color === focusRgb &&
    !transparent(ring.color) &&
    (ring.offset === '2px' || ring.offset === '-2px');
  if (outlineOk) return '';
  const shadowOk =
    ring.shadow &&
    ring.shadow !== 'none' &&
    ring.shadow.includes(focusRgb) &&
    /\b0px 0px 0px 2px\b/.test(ring.shadow);
  if (shadowOk) return '';
  return (
    `outline ${ring.style} ${ring.width} ${ring.color} offset ${ring.offset}, ` +
    `box-shadow ${ring.shadow} (bearer ${ring.bearer}); want 2px solid ${focusRgb} offset 2px`
  );
}

/** Page and main do not scroll sideways; every tagged control is inside the viewport. Runs in the page. */
function measureFit() {
  const se = document.scrollingElement;
  const main = document.querySelector('main.app-main');
  const vw = document.documentElement.clientWidth;
  const outside = [];
  for (const el of document.querySelectorAll('[data-tn-ctl]')) {
    // Controls inside the section strip scroll in their own box at 390.
    if (el.closest('.settings-tab-list')) continue;
    const r = el.getBoundingClientRect();
    if (r.left >= -0.5 && r.right <= vw + 0.5) continue;
    const name = (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 30);
    outside.push(`${el.tagName.toLowerCase()} "${name}" left ${r.left.toFixed(1)} right ${r.right.toFixed(1)}`);
  }
  const strip = document.querySelector('.settings-tab-list');
  const sr = strip?.getBoundingClientRect();
  return {
    page: { scrollWidth: se.scrollWidth, clientWidth: se.clientWidth },
    main: main ? { scrollWidth: main.scrollWidth, clientWidth: main.clientWidth } : null,
    strip: sr ? { left: sr.left, right: sr.right, vw } : null,
    outside,
  };
}

test.describe('terminal-native settings', () => {
  test('shoots the settings sections across the matrix', async ({ page }) => {
    test.setTimeout(300_000);
    for (const route of ROUTES) {
      const files = await shootMatrix(page, {
        slice: 'settings',
        name: route.name,
        path: route.path,
        mockOverrides: OVERRIDES,
      });
      expect(files, `${route.name}: 8 PNGs`).toHaveLength(8);
    }
  });

  test('section labels are mono and the shell copy comes from tnSettings', async ({ page }) => {
    for (const locale of LOCALES) {
      const cell = await openRoute(page, { path: '/settings', locale, mockOverrides: OVERRIDES });
      try {
        await assertRenderedLocale(cell, locale, `/settings ${locale}:`);
        const eyebrow = cell.locator('.settings-nav-brand .settings-eyebrow');
        await expect(eyebrow).toHaveText(locale === 'zh-CN' ? 'conductor / 设置' : 'conductor / settings');
        const font = await eyebrow.evaluate((el) => getComputedStyle(el).fontFamily);
        expect(font).toMatch(/JetBrains Mono|monospace/);
        const size = await eyebrow.evaluate((el) => getComputedStyle(el).fontSize);
        expect(size).toBe('12px');
      } finally {
        await cell.close();
      }
    }
  });

  for (const route of ROUTES) {
    for (const theme of THEMES) {
      test(`${route.path} ${theme}: Tab reaches every primary control and each wears the 2px ink.focus ring`, async ({
        page,
      }) => {
        test.setTimeout(90_000);
        const cell = await openRoute(page, {
          path: route.path,
          theme,
          locale: 'en-US',
          mockOverrides: OVERRIDES,
        });
        try {
          await assertRenderedTheme(cell, theme, route.path);

          const focusRgb = await cell.evaluate(resolveInkFocus);
          expect(focusRgb, `${route.path} ${theme}: --ink-focus on :root`).toBe(INK_FOCUS_RGB[theme]);

          const controls = await cell.evaluate(tagPrimaryControls);
          expect(controls.length, `${route.path}: primary controls found`).toBeGreaterThan(0);

          await cell.evaluate(() => {
            document.activeElement?.blur();
            window.scrollTo(0, 0);
          });
          const reached = new Map();
          const MAX = 300;
          for (let i = 0; i < MAX; i += 1) {
            await cell.keyboard.press('Tab');
            const ring = await cell.evaluate(focusedRing);
            if (ring.seen || (ring.body && i > 0)) break; // wrapped
            if (ring.ctl == null) continue; // a shell control outside the route
            reached.set(ring.ctl, ring);
          }

          const unreached = controls.filter((c) => !reached.has(String(c.i))).map((c) => c.desc);
          expect(unreached, `${route.path} ${theme}: controls Tab never reached`).toEqual([]);

          const bad = [];
          for (const c of controls) {
            const ring = reached.get(String(c.i));
            const problem = ringProblem(ring, focusRgb);
            if (problem) bad.push(`${c.desc}: ${problem}`);
          }
          expect(bad, `${route.path} ${theme}: controls without the ink.focus ring`).toEqual([]);
        } finally {
          await cell.close();
        }
      });
    }
  }

  for (const route of ROUTES) {
    for (const locale of LOCALES) {
      test(`${route.path} 390px ${locale}: no horizontal scroll and every control inside the viewport`, async ({
        page,
      }) => {
        const cell = await openRoute(page, {
          path: route.path,
          width: 390,
          locale,
          mockOverrides: OVERRIDES,
        });
        try {
          await assertRenderedLocale(cell, locale, `${route.path} 390 ${locale}:`);
          const controls = await cell.evaluate(tagPrimaryControls);
          expect(controls.length, `${route.path}: primary controls found`).toBeGreaterThan(0);

          const m = await cell.evaluate(measureFit);
          const where = `${route.path} 390 ${locale}`;
          expect(m.page.scrollWidth, `${where}: page scrolls sideways ${JSON.stringify(m.page)}`).toBeLessThanOrEqual(
            m.page.clientWidth,
          );
          expect(m.main, `${where}: main.app-main`).toBeTruthy();
          expect(m.main.scrollWidth, `${where}: main.app-main overflows ${JSON.stringify(m.main)}`).toBeLessThanOrEqual(
            m.main.clientWidth,
          );
          // The section strip itself stays inside the viewport and scrolls in its own box.
          expect(m.strip, `${where}: section strip`).toBeTruthy();
          expect(m.strip.left, `${where}: strip left`).toBeGreaterThanOrEqual(-0.5);
          expect(m.strip.right, `${where}: strip right`).toBeLessThanOrEqual(m.strip.vw + 0.5);
          expect(m.outside, `${where}: controls outside the viewport`).toEqual([]);
        } finally {
          await cell.close();
        }
      });
    }
  }

  test("reducedMotion 'reduce' leaves no transition or animation duration > 0", async ({ page }) => {
    for (const route of ROUTES) {
      const cell = await openRoute(page, {
        path: route.path,
        reducedMotion: 'reduce',
        mockOverrides: OVERRIDES,
      });
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
              const durations = [...parseSeconds(cs.transitionDuration), ...parseSeconds(cs.animationDuration)];
              if (durations.some((d) => d > 0)) {
                const id = el.id ? `#${el.id}` : '';
                const cls = el.classList?.length ? `.${[...el.classList].join('.')}` : '';
                bad.push(
                  `${el.tagName.toLowerCase()}${id}${cls}${pseudo ?? ''} ` +
                    `transition ${cs.transitionDuration} animation ${cs.animationDuration}`,
                );
              }
            }
          }
          return bad.slice(0, 20);
        });
        expect(offenders, `${route.path}: durations > 0 under reduced motion`).toEqual([]);
      } finally {
        await cell.close();
      }
    }
  });

  test('the section strip switches the panel from the keyboard', async ({ page }) => {
    const cell = await openRoute(page, { path: '/settings', locale: 'en-US', mockOverrides: OVERRIDES });
    try {
      const nav = cell.getByRole('navigation', { name: 'Settings sections' });
      const tabs = nav.getByRole('button');
      await expect(tabs).toHaveCount(6);
      await tabs.nth(5).focus();
      await cell.keyboard.press('Enter');
      await expect(tabs.nth(5)).toHaveAttribute('aria-current', 'page');
      await expect(cell).toHaveURL(/tab=about/);
    } finally {
      await cell.close();
    }
  });
});
