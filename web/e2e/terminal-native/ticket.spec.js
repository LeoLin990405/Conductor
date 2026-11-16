/**
 * S16 acceptance for the terminal-native Ticket page (/tasks/:ticketId, web/ticket.html):
 *
 *  - the screenshot matrix for a running and a completed Ticket (in zh-CN
 *    cells the Ticket comes back with a long CJK title, so both languages are
 *    shot with realistic copy);
 *  - ids and branch names are mono and never overflow at 390 in either language;
 *  - Tab reaches each primary control and each wears the 2px ink.focus ring;
 *  - under reducedMotion 'reduce' no transition or animation duration is > 0;
 *  - an Enter while an input method is composing never sends.
 *
 * Colours/geometry come from the S01 tokens; the matrix helper is import-only.
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

/** 60 CJK Unified Ideographs: the longest realistic title. */
const CJK_TITLE = Array.from({ length: 60 }, (_, i) => String.fromCharCode(0x4e00 + i)).join('');
const LONG_BRANCH = 'line/review-merge-rehearsal-in-a-throwaway-worktree-with-compare-and-swap-update-ref';

const RUNNING_ID = '3419e39e-ca69-4838-844d-547da6333dfc';
const COMPLETED_ID = '7c1e0a44-9b52-4d7e-8f31-5a6c2e90b1d4';

const ticket = (overrides) => ({
  ticket_id: RUNNING_ID,
  user_id: 'u-1',
  workspace_id: 'ws-1',
  title: '#57 Merge rehearsal runs the tests in a throwaway worktree',
  scope:
    'Merge rehearsal runs the tests in a throwaway worktree and lands only the rehearsed commit through a compare-and-swap git update-ref.',
  status: 'running',
  mandate: {},
  origin_trigger_id: null,
  source_id: 'src-1',
  trigger_id: 'trg-91ab77c0',
  origin_thread_key: 'github:owner/repo#57',
  router_thread_id: null,
  worker_thread_id: 'thr-1',
  worker_run_id: null,
  worker_provider: 'deepseek',
  worker_model: 'deepseek-v4-pro',
  worker_daemon_id: null,
  switches: [
    { from: { provider: 'kimi', model: 'kimi-k3-coding' }, to: { provider: 'deepseek', model: 'deepseek-v4-pro' }, reason: 'stall 240s' },
  ],
  attempts: 2,
  attention: null,
  summary: '',
  error: '',
  speculative_outcome: null,
  worktree_path: '/srv/conductor/dispatch/ws/review-merge',
  worktree_branch: LONG_BRANCH,
  worktree_base_sha: 'b0f4d2189a7c3e1100aa',
  worktree_host: 'server',
  worktree_cleanup_pending: false,
  created_at: '2026-11-10T09:12:00Z',
  updated_at: '2026-11-10T09:17:00Z',
  started_at: '2026-11-10T09:12:30Z',
  ended_at: null,
  closed_at: null,
  close_reason: '',
  inputs: [],
  reports: [],
  ...overrides,
});

const RUNNING = ticket({});
const COMPLETED = ticket({
  ticket_id: COMPLETED_ID,
  status: 'completed',
  ended_at: '2026-11-10T09:21:00Z',
  summary: 'The rehearsal now reads the reviewed commit instead of HEAD.',
  reports: [
    {
      report_id: 'r-1',
      state: 'ready',
      summary: 'The rehearsal now reads the reviewed commit instead of HEAD.',
      body: 'It runs in a throwaway worktree with an isolated index. Only the rehearsed commit is landed, through a compare-and-swap update-ref that fails closed.',
      question: null,
      git_check: null,
      created_at: '2026-11-10T09:20:00Z',
    },
  ],
});

const STEPS_RUNNING = [
  { key: 'created', label: 'Ticket created', state: 'done' },
  { key: 'worker', label: 'Worker running', state: 'active' },
];
const STEPS_DONE = [
  { key: 'created', label: 'Ticket created', state: 'done' },
  { key: 'worker', label: 'Worker running', state: 'done' },
  { key: 'report', label: 'Report written', state: 'done' },
];

const progressFor = (row) => ({
  ticket_id: row.ticket_id,
  status: row.status,
  phase: row.status === 'completed' ? 'completed' : 'running',
  elapsed_seconds: row.status === 'completed' ? 512 : 241,
  steps: row.status === 'completed' ? STEPS_DONE : STEPS_RUNNING,
  summary: row.summary,
  body: null,
  question: '',
  error: '',
});

const activityFor = (row) => ({
  ticket_id: row.ticket_id,
  events: [
    { event_id: 'e1', kind: 'created', at: '2026-11-10T09:12:00Z', summary: 'Rehearse the merge in a throwaway worktree and land only the rehearsed commit.', detail: '', state: '' },
    { event_id: 'e2', kind: 'instruction', at: '2026-11-10T09:14:00Z', summary: 'Also delete the throwaway worktree on every path.', detail: '', state: '' },
    { event_id: 'e3', kind: 'report', at: '2026-11-10T09:17:00Z', summary: '21 passed, 2 skipped. Writing the report now.', detail: '', state: row.status === 'completed' ? 'ready' : 'running' },
  ],
});

const json = (body) => async (route) =>
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });

const overridesFor = (row) => ({
  'GET /features': { features: [] },
  'GET /autonomy/overview': {
    counts: { attention: 0, running: 1, queued: 0, completed: 1, failed: 0 },
    sources: 1,
    enabled_sources: 1,
  },
  'GET /autonomy/sources': {
    sources: [{ source_id: 'src-1', kind: 'github', config: { repository: 'owner/repo' }, state: {}, enabled: true }],
  },
  'GET /autonomy/schedules': { schedules: [] },
  'GET /autonomy/tickets': { tickets: [] },
  [`GET /autonomy/tickets/${row.ticket_id}`]: json(row),
  [`GET /autonomy/tickets/${row.ticket_id}/progress`]: json(progressFor(row)),
  [`GET /autonomy/tickets/${row.ticket_id}/activity`]: json(activityFor(row)),
  [`GET /autonomy/tickets/${row.ticket_id}/raw`]: { thread_id: null, run_id: null, events: [], note: null },
  'POST /autonomy/tickets/*/reply': {},
  'GET /native-agents': { runtimes: [] },
  'GET /dispatch/status/active': [],
});

/**
 * In zh-CN cells the Ticket comes back with a 60-CJK-character title, so the
 * shot shows a long title beside the mono ids; en-US cells keep ASCII (a CJK
 * run there would fail the en-US locale assertion).
 */
const cjkTitleInZh = (row) => async (cell) => {
  await expect(cell.getByTestId('ticket-id')).toHaveText(row.ticket_id);
  const lang = await cell.evaluate(() => document.documentElement.lang);
  if (!/^zh/.test(lang)) return;
  await cell.route(
    (url) => url.pathname.endsWith(`/api/v1/autonomy/tickets/${row.ticket_id}`),
    (route) =>
      route.request().resourceType() === 'document'
        ? route.fallback()
        : json({ ...row, title: CJK_TITLE })(route),
  );
  await cell.reload({ waitUntil: 'domcontentloaded' });
  await expect(cell.getByRole('heading', { name: CJK_TITLE })).toBeVisible({ timeout: 15_000 });
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
    out.push({ i, desc: `${el.tagName.toLowerCase()}${el.className ? `.${el.className}` : ''}` });
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

const CASES = [
  { name: 'running', row: RUNNING },
  { name: 'completed', row: COMPLETED },
];

test.describe('terminal-native ticket', () => {
  for (const { name, row } of CASES) {
    test(`matrix: a ${name} Ticket`, async ({ page }) => {
      test.setTimeout(300_000);
      const files = await shootMatrix(page, {
        slice: 'ticket',
        name: `ticket-${name}`,
        path: `/tasks/${row.ticket_id}`,
        mockOverrides: overridesFor(row),
        prepare: cjkTitleInZh(row),
      });
      expect(files, `ticket ${name}: 8 PNGs`).toHaveLength(8);
    });
  }

  for (const locale of LOCALES) {
    test(`390 ${locale}: ids and branch names stay mono and inside the viewport`, async ({ page }) => {
      test.setTimeout(90_000);
      const row = locale === 'zh-CN' ? { ...RUNNING, title: CJK_TITLE } : RUNNING;
      const cell = await openRoute(page, {
        path: `/tasks/${row.ticket_id}`,
        width: 390,
        locale,
        mockOverrides: overridesFor(row),
      });
      try {
        await assertRenderedLocale(cell, locale, `ticket 390 ${locale}:`);
        await expect(cell.getByTestId('ticket-id')).toHaveText(row.ticket_id);
        await expect(cell.locator('[data-check="branch"] dd')).toHaveText(LONG_BRANCH);
        const report = await cell.evaluate(() => {
          const vw = document.documentElement.clientWidth;
          const bad = [];
          const mono = [];
          for (const el of document.querySelectorAll(
            '.tn-ticket-id, .tn-ticket-code, .tn-ticket-origin, [data-check] dd, [data-tn-ctl]',
          )) {
            const r = el.getBoundingClientRect();
            if (r.left < -0.5 || r.right > vw + 0.5 || el.scrollWidth > el.clientWidth + 1) {
              bad.push(`${el.className} left ${r.left.toFixed(1)} right ${r.right.toFixed(1)} sw ${el.scrollWidth} cw ${el.clientWidth}`);
            }
          }
          for (const el of document.querySelectorAll('.tn-ticket-id, [data-check="branch"] dd')) {
            mono.push(getComputedStyle(el).fontFamily);
          }
          const main = document.querySelector('main.app-main');
          return {
            bad,
            mono,
            main: main && { scrollWidth: main.scrollWidth, clientWidth: main.clientWidth },
          };
        });
        expect(report.bad, `ticket 390 ${locale}: overflowing ids`).toEqual([]);
        for (const family of report.mono) expect(family).toMatch(/JetBrains Mono|monospace/);
        expect(report.main.scrollWidth).toBeLessThanOrEqual(report.main.clientWidth);
      } finally {
        await cell.close();
      }
    });
  }

  for (const theme of THEMES) {
    test(`${theme}: Tab reaches every primary control and each wears the 2px ink.focus ring`, async ({
      page,
    }) => {
      test.setTimeout(120_000);
      const cell = await openRoute(page, {
        path: `/tasks/${RUNNING.ticket_id}`,
        theme,
        locale: 'en-US',
        mockOverrides: overridesFor(RUNNING),
      });
      try {
        await assertRenderedTheme(cell, theme, '/tasks/:id');
        await expect(cell.getByTestId('ticket-composer-input')).toBeVisible();
        const focusRgb = await cell.evaluate(resolveInkFocus);
        expect(focusRgb, `ticket ${theme}: --ink-focus`).toBe(INK_FOCUS_RGB[theme]);

        const controls = await cell.evaluate(tagPrimaryControls);
        expect(controls.length, 'ticket: primary controls found').toBeGreaterThan(0);

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
        expect(unreached, `ticket ${theme}: controls Tab never reached`).toEqual([]);

        const bad = [];
        for (const c of controls) {
          const problem = ringProblem(reached.get(String(c.i)), focusRgb);
          if (problem) bad.push(`${c.desc}: ${problem}`);
        }
        expect(bad, `ticket ${theme}: controls without the ink.focus ring`).toEqual([]);
      } finally {
        await cell.close();
      }
    });
  }

  for (const { name, row } of CASES) {
    test(`reducedMotion 'reduce' (${name}) leaves no transition or animation duration > 0`, async ({ page }) => {
      const cell = await openRoute(page, {
        path: `/tasks/${row.ticket_id}`,
        reducedMotion: 'reduce',
        mockOverrides: overridesFor(row),
      });
      try {
        await expect(cell.getByTestId('ticket-id')).toBeVisible();
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
        expect(offenders, `ticket ${name}: durations > 0 under reduced motion`).toEqual([]);
      } finally {
        await cell.close();
      }
    });
  }

  test('composer: a composing Enter never sends; a bare Enter does', async ({ page }) => {
    test.setTimeout(90_000);
    const cell = await openRoute(page, {
      path: `/tasks/${RUNNING.ticket_id}`,
      locale: 'en-US',
      mockOverrides: overridesFor(RUNNING),
    });
    try {
      const replies = [];
      cell.on('request', (req) => {
        if (req.method() === 'POST' && req.url().endsWith('/reply')) replies.push(req.postDataJSON());
      });
      const composer = cell.getByTestId('ticket-composer-input');
      await composer.fill('first line');
      await composer.press('Shift+Enter');
      await composer.pressSequentially('second line');
      await expect(composer).toHaveValue('first line\nsecond line');
      const cancelled = await composer.evaluate((el) => {
        const ime = new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true, cancelable: true });
        el.dispatchEvent(ime);
        return ime.defaultPrevented;
      });
      expect(cancelled, 'a composing Enter is left to the IME').toBe(false);
      await expect(composer).toHaveValue('first line\nsecond line');
      expect(replies).toEqual([]);

      await composer.press('Enter');
      await expect.poll(() => replies.length).toBe(1);
      expect(replies[0]).toEqual({ text: 'first line\nsecond line' });
    } finally {
      await cell.close();
    }
  });
});
