/**
 * S15 acceptance for the terminal-native Tickets list (/tasks, web/tickets.html):
 *
 *  - the screenshot matrix with every one of the seven states present (in the
 *    zh-CN cells one Ticket carries a 60-CJK-character title);
 *  - a long title truncates with a tooltip and never pushes the state column
 *    out of the viewport, at 1440 and 390, in en-US and zh-CN;
 *  - the light selection-ink rule: on a selected row running repaints its glyph
 *    in fg in light, and keeps its tempo ink in dark;
 *  - j/k move the selection while the list has focus and Enter opens the Ticket;
 *  - Tab reaches every primary control and each wears the 2px ink.focus ring;
 *  - under reducedMotion 'reduce' no transition or animation duration is > 0.
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

/** Exactly 60 CJK Unified Ideographs, the longest realistic title. */
const CJK_TITLE = Array.from({ length: 60 }, (_, i) => String.fromCharCode(0x4e00 + i)).join('');
const LONG_EN_TITLE =
  'Native CLI worker session rotation must carry the compacted handoff, keep the work log and the to-do list, and never drop the last two turns';

const ticket = (overrides) => ({
  ticket_id: '00000000-0000-4000-8000-000000000000',
  user_id: 'u-1',
  workspace_id: 'ws-1',
  title: 'Ticket',
  scope: '',
  status: 'queued',
  mandate: {},
  origin_trigger_id: null,
  source_id: '',
  trigger_id: '',
  origin_thread_key: 'github:owner/repo#56',
  router_thread_id: null,
  worker_thread_id: null,
  worker_run_id: null,
  worker_provider: 'codex',
  worker_model: 'gpt-5.3-codex-spark-oauth',
  worker_daemon_id: null,
  attempts: 1,
  attention: null,
  summary: '',
  error: '',
  speculative_outcome: null,
  created_at: '2026-11-13T10:00:00Z',
  updated_at: '2026-11-13T10:00:00Z',
  started_at: null,
  ended_at: null,
  closed_at: null,
  close_reason: '',
  ...overrides,
});

/** One Ticket per state; running is the newest, so it is the selected row. */
const TICKETS = [
  ticket({
    ticket_id: '3419e39e-ca69-4838-844d-547da6333dfc',
    title: 'Merge rehearsal runs the tests in a throwaway worktree',
    status: 'running',
    worker_provider: 'deepseek',
    worker_model: 'deepseek-v4-pro',
    updated_at: '2026-11-13T10:09:00Z',
  }),
  ticket({
    ticket_id: '2f5c8d41-7a63-4b19-8e07-1c9a4b6d3f28',
    title: 'Reproduce the build failure mentioned in the email',
    status: 'queued',
    origin_thread_key: 'manual:email',
    updated_at: '2026-11-13T10:08:00Z',
  }),
  ticket({
    ticket_id: 'b0f4d218-6c37-4a19-9e05-8d1f7ab3c652',
    title: LONG_EN_TITLE,
    status: 'blocked',
    worker_provider: 'cursor',
    worker_model: 'composer-1',
    worker_daemon_id: 'ci-runner',
    updated_at: '2026-11-13T10:07:00Z',
  }),
  ticket({
    ticket_id: '7c1e0a44-9b52-4d7e-8f31-5a6c2e90b1d4',
    title: 'j and k stay characters in the palette query',
    status: 'waiting',
    attention: { question: 'Keep j/k as characters even when the query is empty?' },
    worker_provider: 'kimi',
    worker_model: 'kimi-k3.1-pro-coding',
    worker_daemon_id: 'studio-mac',
    updated_at: '2026-11-13T10:06:00Z',
  }),
  ticket({
    ticket_id: '5e9a77c1-2d48-4b60-a7f3-9c0d5e1b8f42',
    title: 'The focus ring stays visible at 390px on the web',
    status: 'failed',
    worker_provider: 'grok',
    worker_model: 'grok-4.7-oauth',
    error: 'context_window_exhausted',
    updated_at: '2026-11-13T10:05:00Z',
  }),
  ticket({
    ticket_id: '9a2c6f30-8e14-4c5d-b029-7f3a1d6e5c08',
    title: 'The dispatch planner retry keeps the lines object',
    status: 'completed',
    worker_provider: 'stepfun',
    worker_model: 'step-3.7-flash',
    updated_at: '2026-11-13T10:04:00Z',
  }),
  ticket({
    ticket_id: 'c47a1e92-5b08-4d36-a6f4-8e2b90c7d153',
    title: 'Tidy the earlier experiment notes',
    status: 'cancelled',
    worker_provider: 'zcode',
    worker_model: 'glm-5.3-cn-coding',
    updated_at: '2026-11-13T10:03:00Z',
  }),
];

const STATES = ['queued', 'running', 'blocked', 'needs-you', 'failed', 'completed', 'archived'];

/** Answer GET /autonomy/tickets the way the backend filters it. */
const ticketsRoute = (tickets) => async (route) => {
  const params = new URL(route.request().url()).searchParams;
  let rows = tickets;
  if (params.get('attention') === 'true') rows = rows.filter((t) => t.attention);
  if (params.get('failed') === 'true') rows = rows.filter((t) => ['failed', 'blocked'].includes(t.status));
  else if (params.get('status')) rows = rows.filter((t) => t.status === params.get('status'));
  const offset = Number(params.get('offset') ?? 0);
  rows = offset ? [] : rows;
  await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ tickets: rows }) });
};

const overridesFor = (tickets) => ({
  'GET /features': { features: [] },
  'GET /autonomy/sources': { sources: [] },
  'GET /autonomy/tickets': ticketsRoute(tickets),
  'GET /native-agents': { runtimes: [] },
  'GET /dispatch/status/active': [],
});

const zhTickets = () => TICKETS.map((t) => (t.status === 'blocked' ? { ...t, title: CJK_TITLE } : t));

/** Route the zh-CN cells to the CJK title (a later route wins; the 2.5s refetch picks it up). */
const routeCjkTitleInZh = async (cell) => {
  const lang = await cell.evaluate(() => document.documentElement.lang);
  if (!/^zh/.test(lang)) return false;
  await cell.route(
    (url) => url.pathname.endsWith('/api/v1/autonomy/tickets'),
    (route) => (route.request().resourceType() === 'document' ? route.fallback() : ticketsRoute(zhTickets())(route)),
  );
  await expect(cell.locator('.tn-tix-title', { hasText: CJK_TITLE })).toBeVisible({ timeout: 10_000 });
  return true;
};

/** Per-matrix-cell prepare: every state is on screen before the shot. */
const everyStatePresent = async (cell) => {
  await expect(cell.getByTestId('ticket-row')).toHaveCount(7);
  const states = await cell.$$eval('[data-testid="ticket-row"]', (rows) =>
    rows.map((r) => r.getAttribute('data-state')).sort(),
  );
  expect(states, 'every one of the seven states has a row').toEqual([...STATES].sort());
  for (const state of STATES) {
    const mark = cell.locator(`[data-testid="ticket-row"] .sa-state--${state}`);
    await expect(mark.locator('.sa-state-glyph')).not.toBeEmpty();
    await expect(mark.locator('.sa-state-word')).not.toBeEmpty();
  }
  await routeCjkTitleInZh(cell);
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
    out.push({ i, desc: `${el.tagName.toLowerCase()}${el.dataset.testid ? `[${el.dataset.testid}]` : ''}` });
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

test.describe('terminal-native tickets', () => {
  test('matrix: every one of the seven states', async ({ page }) => {
    test.setTimeout(300_000);
    const files = await shootMatrix(page, {
      slice: 'tickets',
      name: 'tickets',
      path: '/tasks',
      mockOverrides: overridesFor(TICKETS),
      prepare: everyStatePresent,
    });
    expect(files, 'tickets: 8 PNGs').toHaveLength(8);
  });

  for (const locale of LOCALES) {
    for (const width of [1440, 390]) {
      test(`${locale} ${width}: a long title truncates with a tooltip and the state column stays in view`, async ({
        page,
      }) => {
        test.setTimeout(90_000);
        const long = locale === 'zh-CN' ? CJK_TITLE : LONG_EN_TITLE;
        const cell = await openRoute(page, {
          path: '/tasks',
          width,
          locale,
          mockOverrides: overridesFor(locale === 'zh-CN' ? zhTickets() : TICKETS),
        });
        try {
          await assertRenderedLocale(cell, locale, `/tasks ${width} ${locale}:`);
          const title = cell.locator('.tn-tix-title', { hasText: long });
          await expect(title).toBeVisible();
          await expect(title).toHaveAttribute('title', long);

          const layout = await cell.evaluate((text) => {
            const se = document.scrollingElement;
            const vw = document.documentElement.clientWidth;
            const titleEl = [...document.querySelectorAll('.tn-tix-title')].find((el) => el.textContent === text);
            const states = [...document.querySelectorAll('[data-testid="ticket-row"] .sa-state')].map((el) => {
              const r = el.getBoundingClientRect();
              return { left: r.left, right: r.right, width: r.width };
            });
            const cs = getComputedStyle(titleEl);
            return {
              page: { scrollWidth: se.scrollWidth, clientWidth: se.clientWidth },
              vw,
              truncated: titleEl.scrollWidth > titleEl.clientWidth,
              ellipsis: cs.textOverflow,
              states,
            };
          }, long);
          expect(layout.page.scrollWidth, `/tasks ${width} ${locale}: page scrolls sideways`).toBeLessThanOrEqual(
            layout.page.clientWidth,
          );
          expect(layout.truncated, `/tasks ${width} ${locale}: the long title is clipped, not wrapped`).toBe(true);
          expect(layout.ellipsis).toBe('ellipsis');
          for (const s of layout.states) {
            expect(s.width, 'state mark keeps its width').toBeGreaterThan(0);
            expect(s.left, 'state mark inside the viewport').toBeGreaterThanOrEqual(-0.5);
            expect(s.right, 'state mark inside the viewport').toBeLessThanOrEqual(layout.vw + 0.5);
          }
        } finally {
          await cell.close();
        }
      });
    }
  }

  for (const theme of THEMES) {
    test(`${theme}: the selected running row follows the selection-ink rule`, async ({ page }) => {
      test.setTimeout(90_000);
      const cell = await openRoute(page, {
        path: '/tasks',
        theme,
        locale: 'en-US',
        mockOverrides: overridesFor(TICKETS),
      });
      try {
        await assertRenderedTheme(cell, theme, '/tasks');
        const selected = cell.locator('[data-testid="ticket-row"][aria-selected="true"]');
        await expect(selected).toHaveAttribute('data-state', 'running');
        const colors = await cell.evaluate(() => {
          const probe = (css) => {
            const el = document.createElement('span');
            el.style.color = css;
            document.body.append(el);
            const c = getComputedStyle(el).color;
            el.remove();
            return c;
          };
          const bg = (css) => {
            const el = document.createElement('span');
            el.style.backgroundColor = css;
            document.body.append(el);
            const c = getComputedStyle(el).backgroundColor;
            el.remove();
            return c;
          };
          const row = document.querySelector('[data-testid="ticket-row"][aria-selected="true"]');
          const other = document.querySelector('[data-testid="ticket-row"][data-state="completed"]');
          return {
            glyph: getComputedStyle(row.querySelector('.sa-state-glyph')).color,
            rowBg: getComputedStyle(row).backgroundColor,
            unselectedCompleted: getComputedStyle(other.querySelector('.sa-state-glyph')).color,
            fg: probe('var(--ink-fg)'),
            tempo: probe('var(--ink-tempo)'),
            ok: probe('var(--ink-ok)'),
            selection: bg('var(--surface-selection)'),
          };
        });
        expect(colors.rowBg, `${theme}: selected row fill`).toBe(colors.selection);
        expect(colors.glyph, `${theme}: selected running glyph`).toBe(theme === 'light' ? colors.fg : colors.tempo);
        expect(colors.unselectedCompleted, `${theme}: unselected rows keep their ink`).toBe(colors.ok);
      } finally {
        await cell.close();
      }
    });
  }

  test('j/k move the selection while the list has focus; Enter opens the Ticket', async ({ page }) => {
    test.setTimeout(90_000);
    const cell = await openRoute(page, {
      path: '/tasks',
      locale: 'en-US',
      mockOverrides: overridesFor(TICKETS),
    });
    try {
      const selected = cell.locator('[data-testid="ticket-row"][aria-selected="true"]');
      await expect(selected).toHaveAttribute('data-state', 'running');

      // j in the search box is a character, not list motion.
      const search = cell.getByTestId('tickets-search');
      await search.click();
      await search.press('j');
      await expect(search).toHaveValue('j');
      await search.fill('');
      await expect(cell.getByTestId('ticket-row')).toHaveCount(7);
      await expect(selected).toHaveAttribute('data-state', 'running');

      await selected.focus();
      await cell.keyboard.press('j');
      await expect(selected).toHaveAttribute('data-state', 'queued');
      await cell.keyboard.press('j');
      await expect(selected).toHaveAttribute('data-state', 'blocked');
      await cell.keyboard.press('k');
      await expect(selected).toHaveAttribute('data-state', 'queued');
      await cell.keyboard.press('Enter');
      await expect(cell).toHaveURL(/\/tasks\/2f5c8d41-7a63-4b19-8e07-1c9a4b6d3f28$/);
    } finally {
      await cell.close();
    }
  });

  for (const theme of THEMES) {
    test(`${theme}: Tab reaches every primary control and each wears the 2px ink.focus ring`, async ({
      page,
    }) => {
      test.setTimeout(120_000);
      const cell = await openRoute(page, {
        path: '/tasks',
        theme,
        locale: 'en-US',
        mockOverrides: overridesFor(TICKETS),
      });
      try {
        await assertRenderedTheme(cell, theme, '/tasks');
        await expect(cell.getByTestId('ticket-row')).toHaveCount(7);
        const focusRgb = await cell.evaluate(resolveInkFocus);
        expect(focusRgb, `/tasks ${theme}: --ink-focus`).toBe(INK_FOCUS_RGB[theme]);

        const controls = await cell.evaluate(tagPrimaryControls);
        expect(controls.length, '/tasks: primary controls found').toBeGreaterThan(0);
        expect(
          controls.some((c) => c.desc.startsWith('tr')),
          '/tasks: the selected row is a tab stop',
        ).toBe(true);

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
        expect(unreached, `/tasks ${theme}: controls Tab never reached`).toEqual([]);

        const bad = [];
        for (const c of controls) {
          const problem = ringProblem(reached.get(String(c.i)), focusRgb);
          if (problem) bad.push(`${c.desc}: ${problem}`);
        }
        expect(bad, `/tasks ${theme}: controls without the ink.focus ring`).toEqual([]);
      } finally {
        await cell.close();
      }
    });
  }

  test("reducedMotion 'reduce' leaves no transition or animation duration > 0", async ({ page }) => {
    const cell = await openRoute(page, {
      path: '/tasks',
      reducedMotion: 'reduce',
      mockOverrides: overridesFor(TICKETS),
    });
    try {
      await expect(cell.getByTestId('ticket-row')).toHaveCount(7);
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
      expect(offenders, '/tasks: durations > 0 under reduced motion').toEqual([]);
    } finally {
      await cell.close();
    }
  });

  test('no border-radius above radius.card on the page', async ({ page }) => {
    const cell = await openRoute(page, {
      path: '/tasks',
      locale: 'en-US',
      mockOverrides: overridesFor(TICKETS),
    });
    try {
      await expect(cell.getByTestId('ticket-row')).toHaveCount(7);
      const tooRound = await cell.evaluate(() => {
        const bad = [];
        for (const el of document.querySelectorAll('main.app-main *')) {
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
      expect(tooRound, '/tasks: no border-radius above radius.card').toEqual([]);
    } finally {
      await cell.close();
    }
  });
});
