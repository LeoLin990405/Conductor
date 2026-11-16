/**
 * S01 acceptance for the shared screenshot helper and the token contracts
 * every later terminal-native web slice reuses:
 *
 *  - /inbox and /settings shot through the full theme × width × locale matrix;
 *  - Tab reaches every primary control of the route and each wears the 2px
 *    ink.focus ring (SPEC.md: 2px solid, offset 2px, the theme's ink.focus);
 *  - at 390px, in en-US and zh-CN, nothing scrolls or overflows sideways and
 *    every primary control sits inside the viewport;
 *  - under reducedMotion 'reduce' no element has a transition or animation
 *    duration above 0.
 *
 * "Primary controls" are enumerated from the DOM, not hand-picked: every
 * focusable, visible, enabled control inside `main.app-main` (links, buttons,
 * inputs, selects, textareas, tabs and other tab stops). A radio group counts
 * once, as the browser gives it one tab stop.
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
  { name: 'inbox', path: '/inbox' },
  { name: 'settings', path: '/settings' },
];

/** tokens.json ink.focus per theme, as Chromium serializes it. */
const INK_FOCUS_RGB = {
  dark: 'rgb(139, 156, 255)', // #8B9CFF
  light: 'rgb(63, 79, 200)', // #3F4FC8
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
 * The ring the focused control wears, measured on whichever element draws it:
 * the control itself, or the `.rings-within` container that rings for a field
 * that fills it (tokens.css). Runs in the page.
 */
function focusedRing() {
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) {
    return { ctl: null, body: true, seen: false };
  }
  // A second visit to any element means the walk has wrapped.
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
  // A control that draws its own ring as a box-shadow: a 2px spread in ink.focus.
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

test.describe('terminal-native tokens', () => {
  test('shoots /inbox and /settings across the theme × width × locale matrix', async ({
    page,
  }) => {
    test.setTimeout(240_000);
    for (const route of ROUTES) {
      const files = await shootMatrix(page, { slice: 'tokens', ...route });
      expect(files, `${route.name}: 8 PNGs`).toHaveLength(8);
    }
  });

  for (const route of ROUTES) {
    for (const theme of THEMES) {
      test(`${route.path} ${theme}: Tab reaches every primary control and each wears the 2px ink.focus ring`, async ({
        page,
      }) => {
        test.setTimeout(90_000);
        const cell = await openRoute(page, { path: route.path, theme, locale: 'en-US' });
        try {
          await assertRenderedTheme(cell, theme, route.path);

          const focusRgb = await cell.evaluate(resolveInkFocus);
          expect(focusRgb, `${route.path} ${theme}: --ink-focus on :root`).toBe(
            INK_FOCUS_RGB[theme],
          );

          const controls = await cell.evaluate(tagPrimaryControls);
          expect(controls.length, `${route.path}: primary controls found`).toBeGreaterThan(0);

          // Start from the top of the document with no pointer history, then walk
          // the whole tab order once, measuring the ring at every stop.
          await cell.evaluate(() => {
            document.activeElement?.blur();
            window.scrollTo(0, 0);
          });
          const reached = new Map();
          const order = [];
          const MAX = 250;
          for (let i = 0; i < MAX; i += 1) {
            await cell.keyboard.press('Tab');
            const ring = await cell.evaluate(focusedRing);
            if (ring.seen || (ring.body && i > 0)) break; // wrapped
            if (ring.ctl == null) continue; // a shell control outside the route
            reached.set(ring.ctl, ring);
            order.push(ring.ctl);
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
      test(`${route.path} 390px ${locale}: no horizontal scroll or overflow, controls inside the viewport`, async ({
        page,
      }) => {
        const cell = await openRoute(page, { path: route.path, width: 390, locale });
        try {
          await assertRenderedLocale(cell, locale, `${route.path} 390 ${locale}:`);
          const controls = await cell.evaluate(tagPrimaryControls);
          expect(controls.length, `${route.path}: primary controls found`).toBeGreaterThan(0);

          const m = await cell.evaluate(() => {
            const se = document.scrollingElement;
            const main = document.querySelector('main.app-main');
            const tag = (el) =>
              `${el.tagName.toLowerCase()}${el.classList.length ? `.${[...el.classList].join('.')}` : ''}`;
            const box = (el) => ({
              sel: tag(el),
              scrollWidth: el.scrollWidth,
              clientWidth: el.clientWidth,
            });
            // The route root and the containers it is built from: every one must
            // keep its content inside its own width. An inner scroller that
            // scrolls sideways counts too: on a phone that is a page that scrolls
            // sideways inside a box.
            const roots = [
              ...main.querySelectorAll(
                '.route-fade-in, .sa-page, .settings-page, .main, .settings-scroll',
              ),
            ];
            const vw = document.documentElement.clientWidth;
            // A control inside a horizontal scroller meant to scroll (a tab
            // strip) is judged by that scroller's box: the user scrolls the
            // strip, not the page. Anything else, including a control pushed past
            // an overflow:hidden or overflow:visible box, is judged by its own box.
            const strip = (el) => {
              for (let a = el.parentElement; a && a !== main; a = a.parentElement) {
                const ox = getComputedStyle(a).overflowX;
                if ((ox === 'auto' || ox === 'scroll') && a.scrollWidth > a.clientWidth) return a;
              }
              return null;
            };
            // Ancestors wider than the viewport, nearest first: where the CSS
            // fix belongs when a control lands outside.
            const tooWide = (el) => {
              const out = [];
              for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
                const w = a.getBoundingClientRect().width;
                if (w > vw + 0.5) out.push(`${tag(a)} ${w.toFixed(0)}px`);
              }
              return out;
            };
            const outside = [];
            for (const el of document.querySelectorAll('[data-tn-ctl]')) {
              const s = strip(el);
              const r = (s ?? el).getBoundingClientRect();
              if (r.left >= -0.5 && r.right <= vw + 0.5) continue;
              const name = (el.getAttribute('aria-label') || el.textContent || '')
                .trim()
                .slice(0, 30);
              const by = s ? ` (its scroller ${tag(s)})` : '';
              const wide = tooWide(s ?? el);
              outside.push(
                `${tag(el)} "${name}"${by} left ${r.left.toFixed(1)} right ${r.right.toFixed(1)} ` +
                  `> viewport ${vw}${wide.length ? `; wider than viewport: ${wide.join(' < ')}` : ''}`,
              );
            }
            return {
              page: { scrollWidth: se.scrollWidth, clientWidth: se.clientWidth },
              main: box(main),
              roots: roots.map(box),
              outside,
            };
          });

          const where = `${route.path} 390 ${locale}`;
          expect(
            m.page.scrollWidth,
            `${where}: page scrolls sideways ${JSON.stringify(m.page)}`,
          ).toBeLessThanOrEqual(m.page.clientWidth);
          expect(
            m.main.scrollWidth,
            `${where}: ${m.main.sel} overflows ${JSON.stringify(m.main)}`,
          ).toBeLessThanOrEqual(m.main.clientWidth);
          expect(m.roots.length, `${where}: route root under main.app-main`).toBeGreaterThan(0);
          const overflowing = m.roots.filter((r) => r.scrollWidth > r.clientWidth);
          expect(overflowing, `${where}: route containers that overflow sideways`).toEqual([]);
          expect(m.outside, `${where}: controls outside the viewport`).toEqual([]);
        } finally {
          await cell.close();
        }
      });
    }
  }

  test("reducedMotion 'reduce' leaves no transition or animation duration > 0", async ({
    page,
  }) => {
    for (const route of ROUTES) {
      const cell = await openRoute(page, { path: route.path, reducedMotion: 'reduce' });
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
});
