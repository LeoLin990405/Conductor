/**
 * S19 acceptance for the terminal-native Lines page (/lines):
 *
 *  - the screenshot matrix (theme × width × locale) over three lines (running,
 *    needs-you, completed) plus the ungrouped bucket;
 *  - long en-US and zh-CN line names truncate without hiding the state mark
 *    or scrolling the page sideways, at 1440 and 390;
 *  - j/k move between rows only while the list has focus;
 *  - Tab reaches every primary control and each wears the 2px ink.focus ring;
 *  - under reducedMotion 'reduce' no transition or animation duration is > 0;
 *  - square geometry: no border-radius above radius.card.
 *
 * The matrix helper is import-only; mocks go through mockAPI overrides.
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

const INK_FOCUS_RGB = { dark: 'rgb(139, 156, 255)', light: 'rgb(63, 79, 200)' };

/** Exactly 60 CJK Unified Ideographs. */
const CJK_NAME = Array.from({ length: 60 }, (_, i) => String.fromCharCode(0x4e00 + i)).join('');
const LONG_EN_NAME =
  'rebuild-the-dispatch-list-and-the-detail-page-with-the-terminal-native-tokens-and-keep-every-handoff-intact';

const at = (minutesAgo) => new Date(Date.UTC(2026, 10, 13, 12, 0) - minutesAgo * 60_000).toISOString();

const ticket = (id, title, status, extra = {}) => ({
  ticket_id: id,
  title,
  status,
  owner: 'm5-daemon',
  worker_provider: 'kimi',
  attempts: 1,
  updated_at: at(5),
  last_activity_at: at(2),
  ...extra,
});

const line = (id, name, branch, tickets, extra = {}) => ({
  line: {
    line_id: id,
    user_id: 'u-1',
    name,
    branch,
    worktree_path: `/Users/dev/conductor-wt/${id}`,
    status: 'active',
    created_at: at(300),
    updated_at: at(3),
  },
  tickets,
  ticket_count: tickets.length,
  open_count: tickets.filter((t) => !['completed', 'cancelled', 'archived'].includes(t.status)).length,
  last_activity_at: at(2),
  ...extra,
});

const linesPayload = (firstName = 'ob-tickets') => ({
  lines: [
    line('l-1', firstName, 'feat/ob-tickets', [
      ticket('t-1', 'Fix the save button', 'running'),
      ticket('t-2', 'Polish the empty state', 'completed', { worker_provider: 'deepseek' }),
    ]),
    line('l-2', 'scheduler-locks', 'fix/lock-wait-warning', [
      ticket('t-3', 'Warn when a scheduler advisory lock waits past 30s', 'waiting', { worker_provider: 'stepfun' }),
    ]),
    line('l-3', 'native-cli-rotate', 'feat/native-cli-rotate', [
      ticket('t-4', 'Rotate the native CLI context without losing the handoff', 'completed', {
        worker_provider: 'cursor',
      }),
    ]),
  ],
  ungrouped: {
    line: null,
    tickets: [ticket('t-5', 'Triage inbox noise', 'queued', { worker_provider: '', owner: 'cloud' })],
    ticket_count: 1,
    open_count: 1,
    last_activity_at: null,
  },
});

const overridesFor = (payload) => ({
  'GET /features': { features: [] },
  'GET /autonomy/tickets/lines': payload,
  'GET /native-agents': { runtimes: [] },
  'GET /dispatch/status/active': [],
});

// ── page-evaluate helpers (self-contained; run in the browser) ──

function tagPrimaryControls() {
  const SEL = [
    'a[href]',
    'button',
    'input:not([type="hidden"])',
    'select',
    'textarea',
    'summary',
    '[role="button"]',
    '[tabindex]',
  ].join(', ');
  const main = document.querySelector('main.app-main');
  if (!main) throw new Error('main.app-main is not on the page');
  const visible = (el) => {
    if (!el.getClientRects().length) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const out = [];
  for (const el of main.querySelectorAll(SEL)) {
    if (el.tabIndex < 0) continue;
    if (el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true') continue;
    if (el.closest('[inert], [aria-hidden="true"]')) continue;
    if (!visible(el)) continue;
    const i = out.length;
    el.setAttribute('data-tn-ctl', String(i));
    out.push({ i, desc: `${el.tagName.toLowerCase()}.${el.className}` });
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
    row: el.getAttribute('data-tn-row'),
    body: false,
    seen,
    style: cs.outlineStyle,
    width: cs.outlineWidth,
    color: cs.outlineColor,
    offset: cs.outlineOffset,
    shadow: cs.boxShadow,
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

function ringProblem(ring, focusRgb) {
  if (!ring) return 'never focused';
  const outlineOk =
    ring.style === 'solid' &&
    ring.width === '2px' &&
    ring.color === focusRgb &&
    (ring.offset === '2px' || ring.offset === '-2px');
  if (outlineOk) return '';
  const shadowOk =
    ring.shadow && ring.shadow !== 'none' && ring.shadow.includes(focusRgb) && /\b0px 0px 0px 2px\b/.test(ring.shadow);
  if (shadowOk) return '';
  return `outline ${ring.style} ${ring.width} ${ring.color} offset ${ring.offset}, box-shadow ${ring.shadow}`;
}

async function assertNoSidewaysScroll(cell, label) {
  const overflow = await cell.evaluate(() => {
    const se = document.scrollingElement;
    const main = document.querySelector('main.app-main');
    return {
      page: { scrollWidth: se.scrollWidth, clientWidth: se.clientWidth },
      main: main && { scrollWidth: main.scrollWidth, clientWidth: main.clientWidth },
      list: [...document.querySelectorAll('.tn-lines, .tn-lines-list, .tn-lines-row')]
        .filter((el) => el.scrollWidth > el.clientWidth + 1)
        .map((el) => el.className),
    };
  });
  expect(overflow.page.scrollWidth, `${label}: page scrolls sideways`).toBeLessThanOrEqual(overflow.page.clientWidth);
  expect(overflow.main.scrollWidth, `${label}: main scrolls sideways`).toBeLessThanOrEqual(overflow.main.clientWidth);
  expect(overflow.list, `${label}: list rows overflow`).toEqual([]);
}

test.describe('terminal-native lines', () => {
  test('matrix: three lines and the ungrouped bucket', async ({ page }) => {
    test.setTimeout(300_000);
    const files = await shootMatrix(page, {
      slice: 'lines',
      name: 'lines',
      path: '/lines',
      mockOverrides: overridesFor(linesPayload()),
      prepare: async (cell) => {
        await expect(cell.locator('[data-tn-row^="line:"]')).toHaveCount(4);
        await expect(cell.locator('[data-tn-row^="ticket:"]')).toHaveCount(5);
      },
    });
    expect(files, 'lines: 8 PNGs').toHaveLength(8);
  });

  for (const [locale, name] of [
    ['en-US', LONG_EN_NAME],
    ['zh-CN', CJK_NAME],
  ]) {
    for (const width of [1440, 390]) {
      test(`${locale} ${width}: a long line name truncates and the state stays visible`, async ({ page }) => {
        test.setTimeout(90_000);
        const cell = await openRoute(page, {
          path: '/lines',
          width,
          locale,
          mockOverrides: overridesFor(linesPayload(name)),
        });
        try {
          await assertRenderedLocale(cell, locale, `lines long ${locale} ${width}:`);
          const row = cell.locator('[data-tn-row^="line:"]').first();
          await expect(row.locator('.tn-lines-name')).toHaveText(name);
          const geo = await row.evaluate((el) => {
            const nameEl = el.querySelector('.tn-lines-name');
            const state = el.querySelector('.tn-lines-state .sa-state');
            const word = el.querySelector('.sa-state-word');
            const glyph = el.querySelector('.sa-state-glyph');
            const r = el.getBoundingClientRect();
            const box = (n) => n.getBoundingClientRect();
            const inside = (n) =>
              box(n).width > 0 && box(n).left >= r.left - 0.5 && box(n).right <= r.right + 0.5;
            return {
              truncated: nameEl.scrollWidth > nameEl.clientWidth,
              ellipsis: getComputedStyle(nameEl).textOverflow,
              stateInside: inside(state),
              glyphInside: inside(glyph),
              wordInside: inside(word),
              wordFits: word.scrollWidth <= word.clientWidth + 1,
              rowRight: r.right,
              vw: document.documentElement.clientWidth,
            };
          });
          expect(geo.truncated, 'the long name is truncated').toBe(true);
          expect(geo.ellipsis).toBe('ellipsis');
          expect(geo.stateInside, 'state mark inside the row').toBe(true);
          expect(geo.glyphInside, 'state glyph inside the row').toBe(true);
          expect(geo.wordInside, 'state word inside the row').toBe(true);
          expect(geo.wordFits, 'state word not clipped').toBe(true);
          expect(geo.rowRight).toBeLessThanOrEqual(geo.vw + 0.5);
          await assertNoSidewaysScroll(cell, `lines long ${locale} ${width}`);
        } finally {
          await cell.close();
        }
      });
    }
  }

  test('j/k move between rows only while the list has focus', async ({ page }) => {
    test.setTimeout(90_000);
    const cell = await openRoute(page, { path: '/lines', mockOverrides: overridesFor(linesPayload()) });
    try {
      const rows = cell.locator('[data-tn-row]');
      await expect(rows).toHaveCount(9);
      const activeRow = () => cell.evaluate(() => document.activeElement?.getAttribute('data-tn-row') ?? null);

      // Outside the list j/k are ignored.
      const refresh = cell.getByRole('button', { name: 'Refresh' });
      await refresh.focus();
      await cell.keyboard.press('j');
      await expect(refresh).toBeFocused();

      await rows.nth(0).focus();
      expect(await activeRow()).toBe('line:l-1');
      await cell.keyboard.press('j');
      expect(await activeRow()).toBe('ticket:t-1');
      await cell.keyboard.press('j');
      expect(await activeRow()).toBe('ticket:t-2');
      await cell.keyboard.press('k');
      expect(await activeRow()).toBe('ticket:t-1');

      // The moved-to row wears the ring and is now the list's tab stop.
      const focusRgb = await cell.evaluate(resolveInkFocus);
      const ring = await cell.evaluate(focusedRing);
      expect(ringProblem(ring, focusRgb), 'ring on the j/k row').toBe('');
      await expect(rows.nth(1)).toHaveAttribute('tabindex', '0');
      await expect(rows.nth(0)).toHaveAttribute('tabindex', '-1');

      // Enter opens the Ticket.
      await cell.keyboard.press('Enter');
      await expect(cell).toHaveURL(/\/tasks\/t-1$/);
    } finally {
      await cell.close();
    }
  });

  for (const theme of THEMES) {
    test(`${theme}: Tab reaches every primary control and each wears the 2px ink.focus ring`, async ({ page }) => {
      test.setTimeout(120_000);
      const cell = await openRoute(page, {
        path: '/lines',
        theme,
        locale: 'en-US',
        mockOverrides: overridesFor(linesPayload()),
      });
      try {
        await assertRenderedTheme(cell, theme, '/lines');
        await expect(cell.locator('[data-tn-row]')).toHaveCount(9);
        const focusRgb = await cell.evaluate(resolveInkFocus);
        expect(focusRgb, `/lines ${theme}: --ink-focus`).toBe(INK_FOCUS_RGB[theme]);

        const controls = await cell.evaluate(tagPrimaryControls);
        expect(controls.map((c) => c.desc).join(' '), '/lines: refresh and the list tab stop').toMatch(
          /tn-lines-button.*tn-lines-row/,
        );

        await cell.evaluate(() => {
          document.activeElement?.blur();
          window.scrollTo(0, 0);
        });
        const reached = new Map();
        for (let i = 0; i < 250; i += 1) {
          await cell.keyboard.press('Tab');
          const ring = await cell.evaluate(focusedRing);
          if (ring.seen || (ring.body && i > 0)) break;
          if (ring.ctl == null) continue;
          reached.set(ring.ctl, ring);
        }
        const unreached = controls.filter((c) => !reached.has(String(c.i))).map((c) => c.desc);
        expect(unreached, `/lines ${theme}: controls Tab never reached`).toEqual([]);
        const bad = controls
          .map((c) => [c.desc, ringProblem(reached.get(String(c.i)), focusRgb)])
          .filter(([, problem]) => problem)
          .map(([desc, problem]) => `${desc}: ${problem}`);
        expect(bad, `/lines ${theme}: controls without the ink.focus ring`).toEqual([]);
      } finally {
        await cell.close();
      }
    });
  }

  test("reducedMotion 'reduce' leaves no transition or animation duration > 0", async ({ page }) => {
    const cell = await openRoute(page, {
      path: '/lines',
      reducedMotion: 'reduce',
      mockOverrides: overridesFor(linesPayload()),
    });
    try {
      await expect(cell.locator('[data-tn-row]')).toHaveCount(9);
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
            if (durations.some((d) => d > 0)) bad.push(`${el.tagName.toLowerCase()}.${el.className}${pseudo ?? ''}`);
          }
        }
        return bad.slice(0, 20);
      });
      expect(offenders, '/lines: durations > 0 under reduced motion').toEqual([]);
    } finally {
      await cell.close();
    }
  });

  for (const locale of LOCALES) {
    for (const theme of THEMES) {
      test(`${theme} ${locale}: square geometry on the Lines page`, async ({ page }) => {
        test.setTimeout(90_000);
        const cell = await openRoute(page, {
          path: '/lines',
          theme,
          locale,
          width: 390,
          mockOverrides: overridesFor(linesPayload()),
        });
        try {
          await expect(cell.locator('[data-tn-row]')).toHaveCount(9);
          const tooRound = await cell.evaluate(() => {
            const bad = [];
            for (const el of document.querySelectorAll('.tn-lines, .tn-lines *')) {
              const cs = getComputedStyle(el);
              for (const corner of [
                'borderTopLeftRadius',
                'borderTopRightRadius',
                'borderBottomLeftRadius',
                'borderBottomRightRadius',
              ]) {
                const v = parseFloat(cs[corner]);
                if (cs[corner].endsWith('px') && Number.isFinite(v) && v > 3.5) {
                  bad.push(`${el.tagName.toLowerCase()}.${el.className} ${corner}=${cs[corner]}`);
                }
              }
            }
            return bad.slice(0, 20);
          });
          expect(tooRound, `/lines ${theme} ${locale}: no border-radius above radius.card`).toEqual([]);
          await assertNoSidewaysScroll(cell, `/lines ${theme} ${locale} 390`);
        } finally {
          await cell.close();
        }
      });
    }
  }
});
