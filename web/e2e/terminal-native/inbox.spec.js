/**
 * S14 acceptance for the terminal-native Inbox (/inbox, web/home.html):
 *
 *  - the screenshot matrix over one needs-you, two running and one failed
 *    Ticket, plus a zh-CN title of 60 CJK characters (a long title must not
 *    clip the reply trap or scroll the page sideways at 390);
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

const ticket = (overrides) => ({
  ticket_id: '7c1e0a44-9b52-4d7e-8f31-5a6c2e90b1d4',
  user_id: 'u-1',
  workspace_id: 'ws-1',
  title: 'Reproduce the build failure mentioned in the email',
  scope: 'Recreate the failing build and capture the log.',
  status: 'running',
  mandate: {},
  origin_trigger_id: null,
  source_id: '',
  trigger_id: '',
  origin_thread_key: 'github:owner/repo#56',
  router_thread_id: null,
  worker_thread_id: null,
  worker_run_id: null,
  worker_provider: 'kimi',
  worker_model: 'kimi-k3.1-pro-coding',
  worker_daemon_id: null,
  attempts: 1,
  attention: null,
  summary: '',
  error: '',
  speculative_outcome: null,
  created_at: '2026-11-13T10:00:00Z',
  updated_at: '2026-11-13T10:12:00Z',
  started_at: '2026-11-13T10:00:00Z',
  ended_at: null,
  closed_at: null,
  close_reason: '',
  ...overrides,
});

const NEEDS_YOU = ticket({
  ticket_id: '6d81b5a9-3f70-4e2c-91ab-40c7f2d8e635',
  title: 'Decide whether to merge the two weekly-report Tickets',
  status: 'blocked',
  worker_provider: 'minimax',
  worker_model: 'minimax-m3',
  attention: { question: 'Two candidates point at the same line — merge into one, or run two in parallel?' },
});

const RUNNING_1 = ticket({
  ticket_id: 'a1b2c3d4-0001-4000-8000-000000000001',
  title: 'Rehearse the merge in a throwaway worktree',
  status: 'running',
  worker_provider: 'deepseek',
  worker_model: 'deepseek-v4-pro',
  summary: 'Running the rehearsal test suite before the compare-and-swap.',
});

const RUNNING_2 = ticket({
  ticket_id: 'a1b2c3d4-0002-4000-8000-000000000002',
  title: 'Warn when a scheduler advisory lock waits past 30s',
  status: 'running',
  worker_provider: 'stepfun',
  worker_model: 'step-3.7-flash',
  summary: 'Structured lock-wait warning carrying the holder dispatch id.',
});

const FAILED = ticket({
  ticket_id: 'a1b2c3d4-0003-4000-8000-000000000003',
  title: 'Rotate the native CLI context without losing the handoff',
  status: 'failed',
  worker_provider: 'cursor',
  worker_model: 'cursor-auto',
  error: 'pytest tests/unit/llms/native_cli -q: 2 failed',
});

const CJK_NEEDS_YOU = ticket({
  ticket_id: 'c0ffee00-0000-4000-8000-000000000000',
  title: CJK_TITLE,
  status: 'blocked',
  attention: { question: CJK_TITLE },
});

/**
 * Answer GET /autonomy/tickets the way the backend filters it: the Inbox asks
 * per category (attention, failed = failed + blocked, or one status), so each
 * query gets only its own rows.
 */
const ticketsRoute = (tickets) => async (route) => {
  const params = new URL(route.request().url()).searchParams;
  let rows = tickets;
  if (params.get('attention') === 'true') rows = rows.filter((t) => t.attention);
  if (params.get('failed') === 'true') rows = rows.filter((t) => ['failed', 'blocked'].includes(t.status));
  else if (params.get('status')) rows = rows.filter((t) => t.status === params.get('status'));
  await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ tickets: rows }) });
};

const overridesFor = (tickets) => ({
  'GET /features': { features: [] },
  'GET /autonomy/overview': {
    counts: { attention: 1, running: 2, queued: 0, completed: 0, failed: 1 },
    sources: 0,
    enabled_sources: 0,
  },
  'GET /autonomy/sources': { sources: [] },
  'GET /autonomy/tickets': ticketsRoute(tickets),
  'GET /native-agents': { runtimes: [] },
  'GET /dispatch/status/active': [],
});

/**
 * Per-matrix-cell prepare: in the zh-CN cells, the needs-you Ticket comes back
 * from the API with the 60-CJK-character title (and question), so React itself
 * renders the long title in the screenshot alongside the four-Ticket mix. The
 * route is added after load (a later route wins) and the page's own 2.5s
 * refetch picks it up. en-US cells keep the short titles (a CJK run there would
 * fail the en-US no-CJK locale assertion).
 */
const showCjkTitleInZh = async (cell) => {
  // One question + one question-free failed Ticket: the needs-you union is 2.
  await expect(cell.locator('#sa-needs-you .sa-count')).toHaveText('◆ 2');
  const lang = await cell.evaluate(() => document.documentElement.lang);
  if (!/^zh/.test(lang)) return;
  const zhTickets = [
    { ...NEEDS_YOU, title: CJK_TITLE, attention: { question: CJK_TITLE } },
    RUNNING_1,
    RUNNING_2,
    FAILED,
  ];
  await cell.route(
    (url) => url.pathname.endsWith('/api/v1/autonomy/tickets'),
    (route) => (route.request().resourceType() === 'document' ? route.fallback() : ticketsRoute(zhTickets)(route)),
  );
  // Confirm the long title actually rendered on the needs-you card, so the
  // shot captures it rather than silently writing a PNG without it.
  await expect(
    cell.locator('.sa-inbox-grid .sa-inbox-title .sa-link', { hasText: CJK_TITLE }),
  ).toBeVisible({ timeout: 10_000 });
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
    out.push({ i, desc: el.tagName.toLowerCase() });
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

test.describe('terminal-native inbox', () => {
  test('matrix: one needs-you, two running, one failed Ticket', async ({ page }) => {
    test.setTimeout(300_000);
    const files = await shootMatrix(page, {
      slice: 'inbox',
      name: 'home',
      path: '/inbox',
      mockOverrides: overridesFor([NEEDS_YOU, RUNNING_1, RUNNING_2, FAILED]),
      prepare: showCjkTitleInZh,
    });
    expect(files, 'inbox: 8 PNGs').toHaveLength(8);
  });

  for (const width of [1440, 390]) {
    test(`a 60-character CJK title fits at ${width} in zh-CN`, async ({ page }) => {
      test.setTimeout(90_000);
      const cell = await openRoute(page, {
        path: '/inbox',
        width,
        locale: 'zh-CN',
        mockOverrides: overridesFor([CJK_NEEDS_YOU]),
      });
      try {
        await assertRenderedLocale(cell, 'zh-CN', `inbox cjk ${width}:`);
        const title = await cell.getByText(CJK_TITLE, { exact: false }).first();
        await expect(title).toBeVisible();

        const overflow = await cell.evaluate(() => {
          const se = document.scrollingElement;
          const main = document.querySelector('main.app-main');
          const roots = main
            ? [...main.querySelectorAll('.route-fade-in, .sa-page, .settings-page, .main')]
            : [];
          return {
            page: { scrollWidth: se.scrollWidth, clientWidth: se.clientWidth },
            main: main && { scrollWidth: main.scrollWidth, clientWidth: main.clientWidth },
            overflowing: roots
              .filter((el) => el.scrollWidth > el.clientWidth)
              .map((el) => el.className),
          };
        });
        expect(
          overflow.page.scrollWidth,
          `inbox cjk ${width}: page scrolls sideways ${JSON.stringify(overflow.page)}`,
        ).toBeLessThanOrEqual(overflow.page.clientWidth);
        expect(
          overflow.main.scrollWidth,
          `inbox cjk ${width}: main.app-main scrolls sideways ${JSON.stringify(overflow.main)}`,
        ).toBeLessThanOrEqual(overflow.main.clientWidth);
        expect(overflow.overflowing, `inbox cjk ${width}: route containers overflow`).toEqual([]);
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
        path: '/inbox',
        theme,
        locale: 'en-US',
        mockOverrides: overridesFor([NEEDS_YOU, RUNNING_1, RUNNING_2, FAILED]),
      });
      try {
        await assertRenderedTheme(cell, theme, '/inbox');
        const focusRgb = await cell.evaluate(resolveInkFocus);
        expect(focusRgb, `/inbox ${theme}: --ink-focus`).toBe(INK_FOCUS_RGB[theme]);

        const controls = await cell.evaluate(tagPrimaryControls);
        expect(controls.length, '/inbox: primary controls found').toBeGreaterThan(0);

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
        expect(unreached, `/inbox ${theme}: controls Tab never reached`).toEqual([]);

        const bad = [];
        for (const c of controls) {
          const problem = ringProblem(reached.get(String(c.i)), focusRgb);
          if (problem) bad.push(`${c.desc}: ${problem}`);
        }
        expect(bad, `/inbox ${theme}: controls without the ink.focus ring`).toEqual([]);
      } finally {
        await cell.close();
      }
    });
  }

  test("reducedMotion 'reduce' leaves no transition or animation duration > 0", async ({ page }) => {
    const cell = await openRoute(page, {
      path: '/inbox',
      reducedMotion: 'reduce',
      mockOverrides: overridesFor([NEEDS_YOU, RUNNING_1, RUNNING_2, FAILED]),
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
      expect(offenders, '/inbox: durations > 0 under reduced motion').toEqual([]);
    } finally {
      await cell.close();
    }
  });

  for (const locale of LOCALES) {
    test(`390px ${locale}: controls sit inside the viewport`, async ({ page }) => {
      const cell = await openRoute(page, {
        path: '/inbox',
        width: 390,
        locale,
        mockOverrides: overridesFor([NEEDS_YOU, RUNNING_1, RUNNING_2, FAILED]),
      });
      try {
        await assertRenderedLocale(cell, locale, `/inbox 390 ${locale}:`);
        const controls = await cell.evaluate(tagPrimaryControls);
        expect(controls.length, '/inbox: primary controls found').toBeGreaterThan(0);
        const outside = await cell.evaluate(() => {
          const vw = document.documentElement.clientWidth;
          const out = [];
          for (const el of document.querySelectorAll('[data-tn-ctl]')) {
            const r = el.getBoundingClientRect();
            if (r.left >= -0.5 && r.right <= vw + 0.5) continue;
            out.push(`${el.tagName.toLowerCase()} left ${r.left.toFixed(1)} right ${r.right.toFixed(1)}`);
          }
          return out;
        });
        expect(outside, `/inbox 390 ${locale}: controls outside the viewport`).toEqual([]);
      } finally {
        await cell.close();
      }
    });
  }

  test('composer: "/" at column 0 opens the command palette', async ({ page }) => {
    test.setTimeout(90_000);
    const cell = await openRoute(page, {
      path: '/inbox',
      locale: 'en-US',
      mockOverrides: overridesFor([NEEDS_YOU, RUNNING_1, RUNNING_2, FAILED]),
    });
    try {
      const composer = cell.getByTestId('inbox-composer-input');
      await composer.fill('');
      await composer.press('/');
      await expect(cell.getByRole('dialog')).toBeVisible();
    } finally {
      await cell.close();
    }
  });

  test('composer: "/" typed mid-word stays ordinary text and opens no palette', async ({ page }) => {
    test.setTimeout(90_000);
    const cell = await openRoute(page, {
      path: '/inbox',
      locale: 'en-US',
      mockOverrides: overridesFor([NEEDS_YOU, RUNNING_1, RUNNING_2, FAILED]),
    });
    try {
      const composer = cell.getByTestId('inbox-composer-input');
      await composer.click();
      await composer.pressSequentially('abc');
      await composer.press('/');
      await expect(cell.getByRole('dialog')).toHaveCount(0);
    } finally {
      await cell.close();
    }
  });

  for (const theme of THEMES) {
    test(`${theme}: square geometry and the danger ink on failed text`, async ({ page }) => {
      test.setTimeout(90_000);
      const cell = await openRoute(page, {
        path: '/inbox',
        theme,
        locale: 'en-US',
        mockOverrides: overridesFor([NEEDS_YOU, RUNNING_1, RUNNING_2, FAILED]),
      });
      try {
        await assertRenderedTheme(cell, theme, '/inbox');

        const geometry = await cell.evaluate(() => {
          const probeColor = (css) => {
            const el = document.createElement('span');
            el.style.color = css;
            document.body.append(el);
            const c = getComputedStyle(el).color;
            el.remove();
            return c;
          };
          const probeRadius = (css) => {
            const el = document.createElement('div');
            el.style.borderRadius = css;
            document.body.append(el);
            const r = getComputedStyle(el).borderTopLeftRadius;
            el.remove();
            return parseFloat(r);
          };
          const measured = (cls) => {
            const el = document.createElement('p');
            el.className = cls;
            el.textContent = 'probe';
            document.body.append(el);
            const cs = getComputedStyle(el);
            const out = { color: cs.color, radius: parseFloat(cs.borderTopLeftRadius) };
            el.remove();
            return out;
          };
          const status = document.createElement('span');
          status.className = 'sa-status sa-status--bad';
          document.body.append(status);
          const statusColor = getComputedStyle(status).color;
          status.remove();
          return {
            ink: probeColor('var(--ink-error)'),
            soft: probeColor('var(--color-danger-soft)'),
            radiusCard: probeRadius('var(--radius-card)'),
            error: measured('sa-error'),
            statusBad: statusColor,
          };
        });

        // The wash must differ from the ink, or the guard below is vacuous.
        expect(geometry.soft, `/inbox ${theme}: danger wash differs from the ink`).not.toBe(geometry.ink);
        // Failed text wears the danger ink, never the danger wash (AUDIT §6 #6).
        expect(geometry.error.color, `/inbox ${theme}: .sa-error foreground`).toBe(geometry.ink);
        expect(geometry.error.color, `/inbox ${theme}: .sa-error is not the wash`).not.toBe(geometry.soft);
        expect(geometry.statusBad, `/inbox ${theme}: .sa-status--bad foreground`).toBe(geometry.ink);
        // Square geometry: nothing above radius.card (AUDIT §6 #5).
        expect(
          geometry.error.radius,
          `/inbox ${theme}: .sa-error radius <= radius.card`,
        ).toBeLessThanOrEqual(geometry.radiusCard);

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
              if (Number.isFinite(v) && v > 3.5) {
                bad.push(`${el.tagName.toLowerCase()}.${el.className} ${corner}=${raw}`);
              }
            }
          }
          return bad.slice(0, 20);
        });
        expect(tooRound, `/inbox ${theme}: no border-radius above radius.card`).toEqual([]);
      } finally {
        await cell.close();
      }
    });
  }
});
