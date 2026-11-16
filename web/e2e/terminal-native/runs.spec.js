/**
 * S20 acceptance for the terminal-native Runs page (/runs):
 *
 *  - the screenshot matrix (theme × width × locale) over one run per state
 *    (queued, running, blocked, needs-you, failed, completed, archived), with
 *    the selected run's raw log beside the list;
 *  - long en-US and zh-CN run titles truncate without hiding the state mark
 *    or the duration, and without scrolling the page sideways, at 1440 and 390;
 *  - durations are right-aligned tabular figures;
 *  - j/k move between run rows; Tab reaches every primary control and each
 *    wears the 2px ink.focus ring;
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
const CJK_TITLE = Array.from({ length: 60 }, (_, i) => String.fromCharCode(0x4e00 + i)).join('');
const LONG_EN_TITLE =
  'Rebuild the dispatch list and the detail page with the terminal-native tokens and keep every handoff intact';

const at = (minutesAgo) => new Date(Date.UTC(2026, 10, 13, 12, 0) - minutesAgo * 60_000).toISOString();

const ticket = (id, title, status, extra = {}) => ({
  ticket_id: id,
  user_id: 'u-1',
  workspace_id: 'ws-1',
  title,
  scope: '',
  status,
  mandate: {},
  origin_trigger_id: null,
  origin_thread_key: 'github:owner/repo#56',
  router_thread_id: null,
  worker_thread_id: `thread-${id}`,
  worker_run_id: null,
  worker_provider: 'kimi',
  worker_model: 'kimi-k3.1-pro-coding',
  worker_daemon_id: null,
  attempts: 1,
  attention: null,
  summary: '',
  error: '',
  speculative_outcome: null,
  created_at: at(90),
  updated_at: at(5),
  started_at: at(30),
  ended_at: null,
  closed_at: null,
  close_reason: '',
  ...extra,
});

const RUNS = [
  ticket('a1b2c3d4-0001-4000-8000-000000000001', 'Rehearse the merge in a throwaway worktree', 'running', {
    worker_provider: 'deepseek',
    worker_model: 'deepseek-v4-pro',
    started_at: new Date(Date.now() - 95_000).toISOString(),
  }),
  ticket('a1b2c3d4-0002-4000-8000-000000000002', 'Decide whether to merge the weekly-report Tickets', 'waiting', {
    worker_provider: 'minimax',
    worker_model: 'minimax-m3',
    attention: { question: 'Two candidates point at the same line: merge into one, or run two?' },
    ended_at: at(12),
  }),
  ticket('a1b2c3d4-0003-4000-8000-000000000003', 'Rotate the native CLI context without losing the handoff', 'failed', {
    worker_provider: 'cursor',
    worker_model: 'cursor-auto',
    started_at: at(80),
    ended_at: at(17),
  }),
  ticket('a1b2c3d4-0004-4000-8000-000000000004', 'Warn when a scheduler advisory lock waits past 30s', 'blocked', {
    worker_provider: 'stepfun',
    worker_model: 'step-3.7-flash',
    started_at: at(40),
    ended_at: at(36),
  }),
  ticket('a1b2c3d4-0005-4000-8000-000000000005', 'Publish the nightly memory digest', 'completed', {
    mandate: { schedule_id: 'sch-1' },
    started_at: at(240),
    ended_at: at(236),
  }),
  ticket('a1b2c3d4-0006-4000-8000-000000000006', 'Triage inbox noise', 'cancelled', {
    origin_thread_key: 'manual:triage',
    started_at: at(500),
    ended_at: at(499),
  }),
  ticket('a1b2c3d4-0007-4000-8000-000000000007', 'Queue the follow-up audit', 'queued', {
    started_at: null,
  }),
];

const RAW = {
  thread_id: 'thread-a1',
  run_id: 'run-a1',
  note: null,
  events: [
    { id: '1731499200000-0', data: '{"type":"status","state":"running"}' },
    { id: '1731499201000-0', data: '{"type":"tool","name":"bash","input":"pytest tests/unit -q"}' },
    { id: '1731499209000-0', data: '{"type":"tool_result","exit":0,"summary":"412 passed"}' },
  ],
};

const overridesFor = (runs) => ({
  'GET /features': { features: [] },
  'GET /autonomy/tickets': { tickets: runs },
  'GET /autonomy/tickets/*/raw': RAW,
  'GET /autonomy/sources': { sources: [] },
  'GET /native-agents': { runtimes: [] },
  'GET /dispatch/status/active': [],
});

/** The zh-CN cells carry the 60-character CJK title on the running run. */
const runsFor = (locale) =>
  /^zh/.test(locale) ? [{ ...RUNS[0], title: CJK_TITLE }, ...RUNS.slice(1)] : RUNS;

// ── page-evaluate helpers (self-contained; run in the browser) ──

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

test.describe('terminal-native runs', () => {
  test('matrix: one run per state with the selected raw log', async ({ page }) => {
    test.setTimeout(300_000);
    const files = await shootMatrix(page, {
      slice: 'runs',
      name: 'runs',
      path: '/runs',
      mockOverrides: overridesFor(RUNS),
      prepare: async (cell) => {
        await expect(cell.locator('[data-tn-run]')).toHaveCount(RUNS.length);
        await expect(cell.locator('.tn-runs-raw')).toContainText('412 passed');
      },
    });
    expect(files, 'runs: 8 PNGs').toHaveLength(8);
  });

  for (const locale of LOCALES) {
    for (const width of [1440, 390]) {
      test(`${locale} ${width}: a long run title truncates; state and duration stay visible`, async ({ page }) => {
        test.setTimeout(90_000);
        const title = /^zh/.test(locale) ? CJK_TITLE : LONG_EN_TITLE;
        const cell = await openRoute(page, {
          path: '/runs',
          width,
          locale,
          mockOverrides: overridesFor([{ ...RUNS[0], title }, ...RUNS.slice(1)]),
        });
        try {
          await assertRenderedLocale(cell, locale, `/runs ${locale} ${width}:`);
          const row = cell.locator('[data-tn-run]').first();
          await expect(row.locator('.tn-runs-title')).toHaveText(title);
          await expect(row.locator('.sa-state')).toBeVisible();
          await expect(row.locator('.tn-runs-duration')).toBeVisible();

          const metrics = await cell.evaluate(() => {
            const se = document.scrollingElement;
            const main = document.querySelector('main.app-main');
            const vw = document.documentElement.clientWidth;
            const r = document.querySelector('[data-tn-run]');
            const box = (el) => el.getBoundingClientRect();
            return {
              page: { scrollWidth: se.scrollWidth, clientWidth: se.clientWidth },
              main: { scrollWidth: main.scrollWidth, clientWidth: main.clientWidth },
              rowRight: box(r).right,
              stateRight: box(r.querySelector('.sa-state')).right,
              durationRight: box(r.querySelector('.tn-runs-duration')).right,
              vw,
              outside: [...main.querySelectorAll('button, [tabindex="0"]')]
                .filter((el) => el.getClientRects().length)
                .filter((el) => box(el).left < -0.5 || box(el).right > vw + 0.5)
                .map((el) => el.className),
            };
          });
          expect(metrics.page.scrollWidth, `/runs ${locale} ${width}: page scrolls sideways`).toBeLessThanOrEqual(
            metrics.page.clientWidth,
          );
          expect(metrics.main.scrollWidth, `/runs ${locale} ${width}: main scrolls sideways`).toBeLessThanOrEqual(
            metrics.main.clientWidth,
          );
          expect(metrics.stateRight).toBeLessThanOrEqual(metrics.rowRight + 0.5);
          expect(metrics.durationRight).toBeLessThanOrEqual(metrics.rowRight + 0.5);
          expect(metrics.outside, `/runs ${locale} ${width}: controls outside the viewport`).toEqual([]);
        } finally {
          await cell.close();
        }
      });
    }
  }

  test('durations are right-aligned tabular figures', async ({ page }) => {
    const cell = await openRoute(page, { path: '/runs', locale: 'en-US', mockOverrides: overridesFor(RUNS) });
    try {
      await expect(cell.locator('[data-tn-run]')).toHaveCount(RUNS.length);
      const cells = await cell.evaluate(() =>
        [...document.querySelectorAll('[data-tn-run] .tn-runs-duration')].map((el) => {
          const cs = getComputedStyle(el);
          return { text: el.textContent, numeric: cs.fontVariantNumeric, align: cs.justifySelf };
        }),
      );
      expect(cells.map((c) => c.text)).toContain('1h 03m');
      expect(cells.map((c) => c.text)).toContain('—');
      for (const c of cells) {
        expect(c.numeric).toContain('tabular-nums');
        expect(c.align).toBe('end');
      }
      const rights = await cell.evaluate(() =>
        [...document.querySelectorAll('[data-tn-run] .tn-runs-duration')].map((el) =>
          Math.round(el.getBoundingClientRect().right),
        ),
      );
      expect(new Set(rights).size, 'durations share one right edge').toBe(1);
    } finally {
      await cell.close();
    }
  });

  test('j/k move between run rows and Enter opens the focused run', async ({ page }) => {
    const cell = await openRoute(page, { path: '/runs', locale: 'en-US', mockOverrides: overridesFor(RUNS) });
    try {
      const rows = cell.locator('[data-tn-run]');
      await expect(rows).toHaveCount(RUNS.length);
      await rows.first().focus();
      await cell.keyboard.press('j');
      await expect(rows.nth(1)).toBeFocused();
      await cell.keyboard.press('j');
      await cell.keyboard.press('k');
      await expect(rows.nth(1)).toBeFocused();
      await cell.keyboard.press('Enter');
      await expect(rows.nth(1)).toHaveAttribute('aria-current', 'true');
      await expect(cell).toHaveURL(new RegExp(`task=${RUNS[1].ticket_id}`));
    } finally {
      await cell.close();
    }
  });

  for (const theme of THEMES) {
    test(`${theme}: Tab reaches every primary control and each wears the 2px ink.focus ring`, async ({ page }) => {
      test.setTimeout(120_000);
      const cell = await openRoute(page, {
        path: '/runs',
        theme,
        locale: 'en-US',
        mockOverrides: overridesFor(RUNS),
      });
      try {
        await assertRenderedTheme(cell, theme, '/runs');
        await expect(cell.locator('.tn-runs-raw')).toContainText('412 passed');
        const focusRgb = await cell.evaluate(resolveInkFocus);
        expect(focusRgb, `/runs ${theme}: --ink-focus`).toBe(INK_FOCUS_RGB[theme]);

        const controls = await cell.evaluate(tagPrimaryControls);
        expect(controls.length, '/runs: primary controls found').toBeGreaterThan(0);
        for (const want of ['tn-runs-run', 'tn-runs-button', 'tn-runs-raw']) {
          expect(controls.some((c) => c.desc.includes(want)), `/runs: ${want} is a primary control`).toBe(true);
        }

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
        expect(unreached, `/runs ${theme}: controls Tab never reached`).toEqual([]);

        const bad = [];
        for (const c of controls) {
          const problem = ringProblem(reached.get(String(c.i)), focusRgb);
          if (problem) bad.push(`${c.desc}: ${problem}`);
        }
        expect(bad, `/runs ${theme}: controls without the ink.focus ring`).toEqual([]);
      } finally {
        await cell.close();
      }
    });
  }

  test("reducedMotion 'reduce' leaves no transition or animation duration > 0", async ({ page }) => {
    const cell = await openRoute(page, {
      path: '/runs',
      reducedMotion: 'reduce',
      mockOverrides: overridesFor(RUNS),
    });
    try {
      await expect(cell.locator('[data-tn-run]')).toHaveCount(RUNS.length);
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
      expect(offenders, '/runs: durations > 0 under reduced motion').toEqual([]);
    } finally {
      await cell.close();
    }
  });

  for (const theme of THEMES) {
    for (const locale of LOCALES) {
      test(`${theme} ${locale}: square geometry on the Runs page`, async ({ page }) => {
        test.setTimeout(90_000);
        const cell = await openRoute(page, {
          path: '/runs',
          theme,
          locale,
          mockOverrides: overridesFor(runsFor(locale)),
        });
        try {
          await assertRenderedTheme(cell, theme, '/runs');
          await expect(cell.locator('[data-tn-run]')).toHaveCount(RUNS.length);
          const tooRound = await cell.evaluate(() => {
            const bad = [];
            for (const el of document.querySelectorAll('main.app-main *')) {
              // Semantic state tags are the one place a pill is allowed.
              if (el.closest('.sa-status')) continue;
              const cs = getComputedStyle(el);
              for (const corner of [
                'borderTopLeftRadius',
                'borderTopRightRadius',
                'borderBottomLeftRadius',
                'borderBottomRightRadius',
              ]) {
                const raw = cs[corner];
                if (!raw.endsWith('px')) continue;
                const v = parseFloat(raw);
                if (Number.isFinite(v) && v > 3.5) bad.push(`${el.tagName.toLowerCase()}.${el.className} ${corner}=${raw}`);
              }
            }
            return bad.slice(0, 20);
          });
          expect(tooRound, `/runs ${theme} ${locale}: no border-radius above radius.card`).toEqual([]);
        } finally {
          await cell.close();
        }
      });
    }
  }
});
