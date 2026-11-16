/**
 * S12 acceptance for the terminal-native shell chrome.
 *
 * `shootMatrix` walks {dark,light} × {1440,390} × {en-US,zh-CN} on /inbox.
 * Each cell proves the correct chrome is mounted (rail at 1440, tab bar at
 * 390), that no nav label is clipped, that Tab reaches every destination and
 * each wears the 2px ink.focus ring, and that the visible palette trigger
 * opens S03's palette and gets focus back on Escape. A separate reduced-motion
 * pass proves the shell adds no transition or animation duration.
 */
import { expect, test } from '../fixtures.js';
import { openRoute, shootMatrix, waitForRenderedRoute } from './matrix.js';

const NAV_KEYS = [
  '/inbox',
  '/tasks',
  '/agents',
  '/lines',
  '/dispatch',
  '/agent-fleet',
  '/runs',
  '/sources',
  '/plugins',
  '/skills-mcp',
  '/settings',
];

/** /inbox endpoints the shell reports against; keep the page from hanging on
 *  requests the default mock set does not answer. */
const INBOX_MOCKS = {
  'GET /features': { features: [] },
  'GET /autonomy/overview': {
    counts: { attention: 0, running: 0, queued: 0, completed: 0, failed: 0 },
    sources: 0,
    enabled_sources: 0,
  },
  'GET /autonomy/sources': { sources: [] },
  'GET /autonomy/tickets': { tickets: [] },
  'GET /native-agents': { runtimes: [] },
  'GET /dispatch/status/active': [],
};

async function inkFocus(page) {
  return page.evaluate(() => {
    const probe = document.createElement('div');
    probe.style.outlineColor = 'var(--ink-focus)';
    document.body.append(probe);
    const color = getComputedStyle(probe).outlineColor;
    probe.remove();
    return color;
  });
}

async function assertNoClippedLabels(page, viewportWidth) {
  const clipped = await page.evaluate(() => {
    const selectors = [
      '.app-nav-item',
      '.app-nav-item-label',
      '.app-nav-palette-label',
      '.bottom-tab-item',
      '.bottom-tab-label',
    ];
    const out = [];
    for (const el of document.querySelectorAll(selectors.join(', '))) {
      if (!el.getClientRects().length) continue;
      if (el.scrollWidth > el.clientWidth + 1) {
        out.push(
          `${el.className} "${(el.textContent || '').trim().slice(0, 40)}" ` +
            `${el.scrollWidth} > ${el.clientWidth}`,
        );
      }
    }
    return out;
  });
  expect(clipped, `${viewportWidth}px: nav labels clipped`).toEqual([]);
}

async function assertTabOrderAndRing(page, focusRgb, viewportWidth) {
  const expected = NAV_KEYS;
  const reached = new Set();
  const rings = new Map();
  const MAX = 80;

  await page.evaluate(() => {
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
    window.scrollTo(0, 0);
  });

  for (let i = 0; i < MAX && reached.size < expected.length; i += 1) {
    await page.keyboard.press('Tab');
    const state = await page.evaluate(() => {
      const el = document.activeElement;
      if (!(el instanceof HTMLElement)) return null;
      const cs = getComputedStyle(el);
      return {
        key: el.getAttribute('data-nav-key'),
        style: cs.outlineStyle,
        width: cs.outlineWidth,
        color: cs.outlineColor,
        offset: cs.outlineOffset,
      };
    });
    if (!state?.key) continue;
    reached.add(state.key);
    rings.set(state.key, state);
  }

  expect([...reached].sort(), `${viewportWidth}px: Tab order reaches`).toEqual(
    [...expected].sort(),
  );

  const bad = [];
  for (const key of expected) {
    const ring = rings.get(key);
    if (
      !ring ||
      ring.style !== 'solid' ||
      ring.width !== '2px' ||
      ring.color !== focusRgb ||
      (ring.offset !== '2px' && ring.offset !== '-2px')
    ) {
      bad.push(`${key}: ${JSON.stringify(ring)}`);
    }
  }
  expect(bad, `${viewportWidth}px: nav controls without the ink.focus ring`).toEqual([]);

  // Leave the shot at the top of the chrome, not scrolled by the last Tab.
  await page.evaluate(() => {
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
    const strip = document.querySelector('.bottom-tab-bar-pill');
    if (strip) strip.scrollLeft = 0;
    window.scrollTo(0, 0);
  });
}

async function assertPaletteTrigger(page, viewportWidth) {
  const trigger = page.getByTestId(
    viewportWidth === 1440 ? 'shell-palette-trigger' : 'tab-palette-trigger',
  );
  await expect(trigger, `${viewportWidth}px: palette trigger`).toBeVisible();
  await expect(trigger).toHaveAttribute('aria-keyshortcuts', /Control\+K/);

  await trigger.click();
  await expect(page.getByTestId('tn-palette-input')).toBeVisible({ timeout: 10_000 });
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('tn-palette')).toBeHidden();
  await expect(trigger, `${viewportWidth}px: Esc returns focus to the trigger`).toBeFocused();
}

test.describe('terminal-native shell', () => {
  test('matrix: /inbox rail at 1440, tab bar at 390, both locales', async ({ page }) => {
    test.setTimeout(360_000);

    const files = await shootMatrix(page, {
      slice: 'shell',
      name: 'shell',
      path: '/inbox',
      mockOverrides: INBOX_MOCKS,
      prepare: async (cell) => {
        const width = cell.viewportSize()?.width;
        if (width === 1440) {
          await expect(cell.getByTestId('app-rail')).toBeVisible();
          await expect(cell.getByTestId('bottom-tab-bar')).toHaveCount(0);
        } else {
          await expect(cell.getByTestId('bottom-tab-bar')).toBeVisible();
          await expect(cell.getByTestId('app-rail')).toHaveCount(0);
        }

        await assertNoClippedLabels(cell, width);

        // Resolve the ring hue per cell: ink.focus differs between themes.
        const focusRgb = await inkFocus(cell);
        await assertTabOrderAndRing(cell, focusRgb, width);
        await assertPaletteTrigger(cell, width);

        // The palette walk is pointer-made; clear the ring before the shot so
        // a keyboard ring does not travel into the next review image.
        await cell.evaluate(() => {
          if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        });
      },
    });

    expect(files).toHaveLength(8);
  });

  test("reducedMotion 'reduce' leaves no transition or animation duration > 0", async ({
    page,
  }) => {
    test.setTimeout(90_000);
    const cell = await openRoute(page, { path: '/inbox', reducedMotion: 'reduce' });
    try {
      await waitForRenderedRoute(cell);
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
              bad.push(
                `${el.tagName.toLowerCase()}${pseudo ?? ''} ` +
                  `${cs.transitionDuration} / ${cs.animationDuration}`,
              );
            }
          }
        }
        return bad;
      });
      expect(offenders, 'shell motion under reduced motion').toEqual([]);
    } finally {
      await cell.close();
    }
  });
});
