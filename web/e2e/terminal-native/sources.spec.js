/**
 * S21 acceptance for the terminal-native Sources page (/sources):
 *
 *  - the screenshot matrix over four polled Sources (healthy, error, paused,
 *    never polled), one scheduled task and one Router-health row, with job
 *    counts per Source from GET /autonomy/tickets?source_id=…;
 *  - en-US and zh-CN fit at 1440 and 390: no sideways scroll, every control
 *    inside the viewport, and the count cells stay right-aligned at 1440;
 *  - Tab reaches every primary control and each wears the 2px ink.focus ring;
 *  - under reducedMotion 'reduce' no transition or animation duration is > 0;
 *  - square geometry: nothing above radius.card.
 *
 * Colours/geometry come from the S01 tokens; the matrix helper is import-only.
 */
import { expect, test } from '../fixtures.js';

import {
  LOCALES,
  THEMES,
  WIDTHS,
  assertRenderedLocale,
  assertRenderedTheme,
  openRoute,
  shootMatrix,
} from './matrix.js';

const NOW = '2026-11-13T10:00:00Z';

const source = (id, repository, { enabled = true, state = {} } = {}) => ({
  source_id: id,
  user_id: 'u-1',
  kind: 'github',
  config: {
    repository,
    secret_name: 'GITHUB_TOKEN',
    workspace_id: 'ws-1',
    allowed_logins: ['leo'],
    poll_seconds: 60,
    worker_provider: '',
    worker_daemon_id: null,
  },
  state,
  enabled,
  created_at: NOW,
  updated_at: NOW,
});

const SOURCES = [
  source('src-ok', 'acme/widgets', { state: { last_poll_at: NOW, agent_login: 'conductor-bot', cursor: '9f2c1a7be0' } }),
  source('src-err', 'acme/a-repository-with-a-rather-long-name-for-wrapping', {
    state: { last_poll_at: NOW, last_error: 'GitHub answered 401: bad credentials for GITHUB_TOKEN' },
  }),
  source('src-off', 'acme/paused', { enabled: false, state: { last_poll_at: NOW } }),
  {
    ...source('src-new', 'oc_7d1e0a449b52', { state: {} }),
    kind: 'feishu',
    config: { ...source('x', '').config, repository: '', chat_id: 'oc_7d1e0a449b52' },
  },
];

const SCHEDULE = {
  schedule_id: '11111111-1111-4111-8111-111111111111',
  user_id: 'u-1',
  workspace_id: 'ws-1',
  title: 'Morning review',
  prompt: 'Review the repository',
  cron_expression: '0 9 * * 1-5',
  timezone: 'UTC',
  enabled: true,
  worker_provider: '',
  worker_model: '',
  worker_daemon_id: null,
  next_run_at: NOW,
  last_run_at: null,
  last_error: '',
  created_at: NOW,
  updated_at: NOW,
};

const EVAL = {
  eval_id: 'eval-1',
  thread_key: 'github:acme/widgets#56',
  reason: 'router unavailable',
  trigger_ids: ['tr-1', 'tr-2'],
};

const ticket = (status, attention = null) => ({
  ticket_id: `t-${Math.random().toString(16).slice(2)}`,
  user_id: 'u-1',
  workspace_id: 'ws-1',
  title: 'work',
  scope: 'work',
  status,
  mandate: {},
  attention,
  created_at: NOW,
  updated_at: NOW,
});

/** Tickets per Source / schedule, the way the backend filters the list. */
const TICKETS = {
  'src-ok': [ticket('running'), ticket('running'), ticket('queued'), ticket('blocked', { question: 'merge?' }), ticket('completed')],
  'src-err': [ticket('failed'), ticket('failed'), ticket('completed')],
  [SCHEDULE.schedule_id]: [ticket('completed')],
};

const ticketsRoute = async (route) => {
  const params = new URL(route.request().url()).searchParams;
  const key = params.get('source_id') || params.get('schedule_id') || '';
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ tickets: TICKETS[key] ?? [] }),
  });
};

const OVERRIDES = {
  'GET /features': { features: [] },
  'GET /autonomy/overview': {
    counts: { attention: 1, running: 2, queued: 1, completed: 3, failed: 2 },
    sources: 4,
    enabled_sources: 3,
  },
  'GET /autonomy/sources': { sources: SOURCES },
  'GET /autonomy/schedules': { schedules: [SCHEDULE] },
  'GET /autonomy/evals': { evals: [EVAL] },
  'GET /autonomy/tickets': ticketsRoute,
  'GET /native-agents': { runtimes: [] },
  'GET /dispatch/status/active': [],
};

/** Wait for the rows and their counts before a shot or a measurement. */
const waitForRows = async (cell) => {
  await expect(cell.locator('.tn-sources-row[data-row="src-ok"]')).toBeVisible();
  await expect(
    cell.locator('.tn-sources-row[data-row="src-ok"] [data-count="running"] .tn-sources-count-num'),
  ).toHaveText('2', { timeout: 10_000 });
};

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
    '[role="combobox"]',
    '[contenteditable="true"]',
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
    out.push({ i, desc: `${el.tagName.toLowerCase()} ${(el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 24)}` });
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
  return `outline ${ring.style} ${ring.width} ${ring.color} offset ${ring.offset}, box-shadow ${ring.shadow}`;
}

const INK_FOCUS_RGB = { dark: 'rgb(139, 156, 255)', light: 'rgb(63, 79, 200)' };

test.describe('terminal-native sources', () => {
  test('matrix: four Sources, one schedule, one Router-health row', async ({ page }) => {
    test.setTimeout(300_000);
    const files = await shootMatrix(page, {
      slice: 'sources',
      name: 'sources',
      path: '/sources',
      mockOverrides: OVERRIDES,
      prepare: waitForRows,
    });
    expect(files, 'sources: 8 PNGs').toHaveLength(8);
  });

  for (const locale of LOCALES) {
    for (const width of WIDTHS) {
      test(`${locale} at ${width}: fits, health is glyph + word, controls stay in view`, async ({ page }) => {
        test.setTimeout(90_000);
        const cell = await openRoute(page, { path: '/sources', width, locale, mockOverrides: OVERRIDES });
        try {
          await assertRenderedLocale(cell, locale, `/sources ${width} ${locale}:`);
          await waitForRows(cell);

          const report = await cell.evaluate(() => {
            const se = document.scrollingElement;
            const main = document.querySelector('main.app-main');
            const vw = document.documentElement.clientWidth;
            const health = [...document.querySelectorAll('.tn-sources-row .tn-sources-health')].map((el) => ({
              glyph: el.querySelector('.tn-sources-health-glyph')?.textContent?.trim() ?? '',
              word: el.querySelector('.tn-sources-health-word')?.textContent?.trim() ?? '',
            }));
            const outside = [];
            for (const el of main.querySelectorAll('button, select, input, textarea, summary')) {
              if (!el.getClientRects().length) continue;
              const r = el.getBoundingClientRect();
              if (r.left >= -0.5 && r.right <= vw + 0.5) continue;
              outside.push(`${el.tagName.toLowerCase()} ${el.textContent?.trim().slice(0, 20)} ${r.left.toFixed(1)}..${r.right.toFixed(1)}`);
            }
            const row = document.querySelector('.tn-sources-row[data-row="src-ok"]');
            const counts = row.querySelector('.tn-sources-cell-counts .tn-sources-counts');
            const total = counts.querySelector('[data-count="total"]');
            return {
              page: { scrollWidth: se.scrollWidth, clientWidth: se.clientWidth },
              main: { scrollWidth: main.scrollWidth, clientWidth: main.clientWidth },
              health,
              outside,
              tabular: getComputedStyle(counts).fontVariantNumeric,
              countsRight: counts.getBoundingClientRect().right,
              rowRight: row.getBoundingClientRect().right,
              totalRight: total.getBoundingClientRect().right,
              rowPadRight: parseFloat(getComputedStyle(row).paddingRight),
            };
          });

          expect(report.page.scrollWidth, `/sources ${width} ${locale}: page scrolls sideways`).toBeLessThanOrEqual(report.page.clientWidth);
          expect(report.main.scrollWidth, `/sources ${width} ${locale}: main scrolls sideways`).toBeLessThanOrEqual(report.main.clientWidth);
          expect(report.outside, `/sources ${width} ${locale}: controls outside the viewport`).toEqual([]);
          expect(report.health.length, 'one health mark per row').toBeGreaterThanOrEqual(6);
          for (const h of report.health) {
            expect(h.glyph, 'health glyph').not.toBe('');
            expect(h.word, 'health word').not.toBe('');
          }
          expect(report.tabular, 'job counts use tabular figures').toContain('tabular-nums');
          if (width === 1440) {
            // Right-aligned: the count block ends at the row's content edge.
            expect(Math.abs(report.rowRight - report.rowPadRight - report.countsRight)).toBeLessThanOrEqual(1);
            expect(Math.abs(report.countsRight - report.totalRight)).toBeLessThanOrEqual(1);
          }
        } finally {
          await cell.close();
        }
      });
    }
  }

  for (const theme of THEMES) {
    test(`${theme}: Tab reaches every primary control and each wears the 2px ink.focus ring`, async ({ page }) => {
      test.setTimeout(150_000);
      const cell = await openRoute(page, { path: '/sources', theme, locale: 'en-US', mockOverrides: OVERRIDES });
      try {
        await assertRenderedTheme(cell, theme, '/sources');
        await waitForRows(cell);
        const focusRgb = await cell.evaluate(resolveInkFocus);
        expect(focusRgb, `/sources ${theme}: --ink-focus`).toBe(INK_FOCUS_RGB[theme]);

        // Controls inside a closed <details> are not tab stops until it opens;
        // open every disclosure so its fields are checked too.
        await cell.evaluate(() => {
          for (const d of document.querySelectorAll('main.app-main details')) d.open = true;
        });
        const controls = await cell.evaluate(tagPrimaryControls);
        expect(controls.length, '/sources: primary controls found').toBeGreaterThan(10);

        await cell.evaluate(() => {
          document.activeElement?.blur();
          window.scrollTo(0, 0);
        });
        const reached = new Map();
        for (let i = 0; i < 400; i += 1) {
          await cell.keyboard.press('Tab');
          const ring = await cell.evaluate(focusedRing);
          if (ring.seen || (ring.body && i > 0)) break;
          if (ring.ctl == null) continue;
          reached.set(ring.ctl, ring);
        }

        const unreached = controls.filter((c) => !reached.has(String(c.i))).map((c) => c.desc);
        expect(unreached, `/sources ${theme}: controls Tab never reached`).toEqual([]);

        const bad = [];
        for (const c of controls) {
          const problem = ringProblem(reached.get(String(c.i)), focusRgb);
          if (problem) bad.push(`${c.desc}: ${problem}`);
        }
        expect(bad, `/sources ${theme}: controls without the ink.focus ring`).toEqual([]);
      } finally {
        await cell.close();
      }
    });
  }

  test("reducedMotion 'reduce' leaves no transition or animation duration > 0", async ({ page }) => {
    const cell = await openRoute(page, { path: '/sources', reducedMotion: 'reduce', mockOverrides: OVERRIDES });
    try {
      await waitForRows(cell);
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
      expect(offenders, '/sources: durations > 0 under reduced motion').toEqual([]);
    } finally {
      await cell.close();
    }
  });

  for (const theme of THEMES) {
    test(`${theme}: square geometry, nothing above radius.card`, async ({ page }) => {
      test.setTimeout(90_000);
      const cell = await openRoute(page, { path: '/sources', theme, locale: 'en-US', mockOverrides: OVERRIDES });
      try {
        await assertRenderedTheme(cell, theme, '/sources');
        await waitForRows(cell);
        const result = await cell.evaluate(() => {
          const probe = document.createElement('div');
          probe.style.borderRadius = 'var(--radius-card)';
          document.body.append(probe);
          const card = parseFloat(getComputedStyle(probe).borderTopLeftRadius);
          probe.remove();
          const bad = [];
          for (const el of document.querySelectorAll('.tn-sources *, .tn-sources')) {
            const cs = getComputedStyle(el);
            for (const corner of ['borderTopLeftRadius', 'borderTopRightRadius', 'borderBottomLeftRadius', 'borderBottomRightRadius']) {
              const raw = cs[corner];
              if (!raw.endsWith('px')) continue;
              const v = parseFloat(raw);
              if (Number.isFinite(v) && v > card + 0.5) bad.push(`${el.tagName.toLowerCase()}.${el.className} ${corner}=${raw}`);
            }
          }
          const err = document.querySelector('.tn-sources-row[data-row="src-err"] .tn-sources-error-line');
          const ink = document.createElement('span');
          ink.style.color = 'var(--ink-error)';
          document.body.append(ink);
          const errorInk = getComputedStyle(ink).color;
          ink.remove();
          return { card, bad: bad.slice(0, 20), errorColor: err && getComputedStyle(err).color, errorInk };
        });
        expect(result.card, 'radius.card').toBe(3);
        expect(result.bad, `/sources ${theme}: no border-radius above radius.card`).toEqual([]);
        expect(result.errorColor, `/sources ${theme}: a Source error wears the danger ink`).toBe(result.errorInk);
      } finally {
        await cell.close();
      }
    });
  }
});
