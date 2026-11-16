/**
 * S18 acceptance for the terminal-native Agents page (/agents):
 *
 *  - the screenshot matrix over the PTC agent, two native runtimes (one
 *    verified, one not), one busy and one offline daemon, a stalled and a busy
 *    Main Agent run and one alert; zh-CN cells carry a 60-CJK-character daemon
 *    name and stall reason, which must not clip actions or scroll sideways;
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
  assertRenderedLocale,
  assertRenderedTheme,
  openRoute,
  shootMatrix,
} from './matrix.js';

/** Exactly 60 CJK Unified Ideographs, the longest realistic label. */
const CJK_LONG = Array.from({ length: 60 }, (_, i) => String.fromCharCode(0x4e00 + i)).join('');

const DAEMON_BUSY = {
  daemon_id: 'd0000001-0000-4000-8000-000000000001',
  name: 'studio-mac',
  provider: 'codex',
  max_concurrency: 2,
  last_seen_at: '2026-11-13T10:00:00Z',
  created_at: '2026-11-01T00:00:00Z',
};

const DAEMON_OFFLINE = {
  daemon_id: 'd0000002-0000-4000-8000-000000000002',
  name: 'old-laptop',
  provider: '',
  max_concurrency: 1,
  last_seen_at: null,
  created_at: '2026-11-01T00:00:00Z',
};

const boardFor = (stallReason) => ({
  agents: [
    {
      run_id: 'run-7f3a',
      task_id: 'task-1',
      ticket_id: 'ticket-56',
      agent_id: 'worker-kimi',
      daemon_id: DAEMON_BUSY.daemon_id,
      state: 'running',
      last_heartbeat: '2026-11-13T10:00:00Z',
      current_step: 'Running the rehearsal test suite',
      stalled: false,
      stall_reason: null,
    },
    {
      run_id: 'run-7f3a',
      task_id: 'task-2',
      ticket_id: null,
      agent_id: 'worker-glm',
      daemon_id: null,
      state: 'running',
      last_heartbeat: '2026-11-13T09:50:00Z',
      current_step: '',
      stalled: true,
      stall_reason: stallReason,
    },
  ],
  alerts: [
    {
      id: 'alert-1',
      run_id: 'run-7f3a',
      task_id: 'task-2',
      severity: 'warning',
      message: 'Worker heartbeat expired',
      created_at: '2026-11-13T09:55:00Z',
    },
  ],
  thresholds: { heartbeat_seconds: 60, progress_seconds: 300 },
});

const overridesFor = ({ daemonName = DAEMON_BUSY.name, stallReason = 'No progress for 5 minutes' } = {}) => ({
  'GET /features': { features: [] },
  'GET /native-agents': {
    runtimes: [
      { provider: 'codex', installed: true, live_verified: true },
      { provider: 'kimi', installed: true, live_verified: false },
    ],
  },
  'GET /native-agents/auth': {
    providers: [
      { provider: 'codex', supported: true, installed: true, authenticated: true },
      { provider: 'kimi', supported: true, installed: true, authenticated: false },
    ],
  },
  'GET /autonomy/metrics': { running_tickets: 1, max_workers: 4 },
  'GET /autonomy/daemons': { daemons: [{ ...DAEMON_BUSY, name: daemonName }, DAEMON_OFFLINE] },
  'GET /main-agent/status-board': boardFor(stallReason),
  'GET /dispatch/status/active': [],
});

/**
 * Per-matrix-cell prepare: wait for the rows, then in zh-CN cells re-answer
 * the daemons and status board with 60-CJK-character copy (a later route
 * wins; the page's own refetch picks it up) and confirm it rendered.
 */
const showCjkInZh = async (cell) => {
  await expect(cell.locator('[data-testid^="daemon-row-"]').first()).toBeVisible({ timeout: 10_000 });
  await expect(cell.locator('[data-testid^="main-agent-run-"]').first()).toBeVisible({ timeout: 10_000 });
  const lang = await cell.evaluate(() => document.documentElement.lang);
  if (!/^zh/.test(lang)) return;
  const zh = overridesFor({ daemonName: CJK_LONG, stallReason: CJK_LONG });
  for (const [key, body] of [
    ['/api/v1/autonomy/daemons', zh['GET /autonomy/daemons']],
    ['/api/v1/main-agent/status-board', zh['GET /main-agent/status-board']],
  ]) {
    await cell.route(
      (url) => url.pathname.endsWith(key),
      (route) =>
        route.request().resourceType() === 'document'
          ? route.fallback()
          : route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) }),
    );
  }
  await expect(cell.locator('.ag-stall', { hasText: CJK_LONG })).toBeVisible({ timeout: 10_000 });
  await expect(cell.locator('.ag-row--daemon .ag-name', { hasText: CJK_LONG })).toBeVisible({ timeout: 20_000 });
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

test.describe('terminal-native agents', () => {
  test('matrix: runtimes, daemons, Main Agent runs and alerts', async ({ page }) => {
    test.setTimeout(300_000);
    const files = await shootMatrix(page, {
      slice: 'agents',
      name: 'agents',
      path: '/agents',
      mockOverrides: overridesFor(),
      prepare: showCjkInZh,
    });
    expect(files, 'agents: 8 PNGs').toHaveLength(8);
  });

  test('rows carry a state glyph and word', async ({ page }) => {
    const cell = await openRoute(page, { path: '/agents', locale: 'en-US', mockOverrides: overridesFor() });
    try {
      await expect(cell.getByTestId('agent-row-ptc').locator('[data-state="online"]')).toBeVisible();
      await expect(cell.getByTestId('agent-row-kimi').locator('[data-state="offline"]')).toBeVisible();
      await expect(cell.getByTestId(`daemon-row-${DAEMON_BUSY.daemon_id}`).locator('[data-state="busy"]')).toBeVisible();
      await expect(cell.getByTestId(`daemon-row-${DAEMON_OFFLINE.daemon_id}`).locator('[data-state="offline"]')).toBeVisible();
      await expect(cell.getByTestId('main-agent-run-run-7f3a-task-2').locator('[data-state="stalled"]')).toContainText('✖');
    } finally {
      await cell.close();
    }
  });

  for (const width of [1440, 390]) {
    test(`60-character CJK daemon name and stall reason fit at ${width} in zh-CN`, async ({ page }) => {
      test.setTimeout(90_000);
      const cell = await openRoute(page, {
        path: '/agents',
        width,
        locale: 'zh-CN',
        mockOverrides: overridesFor({ daemonName: CJK_LONG, stallReason: CJK_LONG }),
      });
      try {
        await assertRenderedLocale(cell, 'zh-CN', `agents cjk ${width}:`);
        await expect(cell.locator('.ag-row--daemon .ag-name', { hasText: CJK_LONG })).toBeVisible();
        await expect(cell.locator('.ag-stall', { hasText: CJK_LONG })).toBeVisible();
        const overflow = await cell.evaluate(() => {
          const se = document.scrollingElement;
          const main = document.querySelector('main.app-main');
          const roots = main ? [...main.querySelectorAll('.sa-page, .ag-section, .ag-rows, .ag-row')] : [];
          const vw = document.documentElement.clientWidth;
          const clipped = [...document.querySelectorAll('main.app-main button, main.app-main input')]
            .filter((el) => el.getClientRects().length)
            .map((el) => el.getBoundingClientRect())
            .filter((r) => r.left < -0.5 || r.right > vw + 0.5).length;
          return {
            page: { scrollWidth: se.scrollWidth, clientWidth: se.clientWidth },
            main: main && { scrollWidth: main.scrollWidth, clientWidth: main.clientWidth },
            overflowing: roots.filter((el) => el.scrollWidth > el.clientWidth + 1).map((el) => el.className),
            clipped,
          };
        });
        expect(overflow.page.scrollWidth, `agents cjk ${width}: page scrolls sideways`).toBeLessThanOrEqual(overflow.page.clientWidth);
        expect(overflow.main.scrollWidth, `agents cjk ${width}: main scrolls sideways`).toBeLessThanOrEqual(overflow.main.clientWidth);
        expect(overflow.overflowing, `agents cjk ${width}: containers overflow`).toEqual([]);
        expect(overflow.clipped, `agents cjk ${width}: controls outside the viewport`).toBe(0);
      } finally {
        await cell.close();
      }
    });
  }

  for (const theme of THEMES) {
    test(`${theme}: Tab reaches every primary control and each wears the 2px ink.focus ring`, async ({ page }) => {
      test.setTimeout(120_000);
      const cell = await openRoute(page, { path: '/agents', theme, locale: 'en-US', mockOverrides: overridesFor() });
      try {
        await assertRenderedTheme(cell, theme, '/agents');
        await expect(cell.locator('[data-testid^="daemon-row-"]').first()).toBeVisible();
        const focusRgb = await cell.evaluate(resolveInkFocus);
        expect(focusRgb, `/agents ${theme}: --ink-focus`).toBe(INK_FOCUS_RGB[theme]);

        const controls = await cell.evaluate(tagPrimaryControls);
        expect(controls.length, '/agents: primary controls found').toBeGreaterThan(0);

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
        expect(unreached, `/agents ${theme}: controls Tab never reached`).toEqual([]);

        const bad = [];
        for (const c of controls) {
          const problem = ringProblem(reached.get(String(c.i)), focusRgb);
          if (problem) bad.push(`${c.desc}: ${problem}`);
        }
        expect(bad, `/agents ${theme}: controls without the ink.focus ring`).toEqual([]);
      } finally {
        await cell.close();
      }
    });
  }

  test("reducedMotion 'reduce' leaves no transition or animation duration > 0", async ({ page }) => {
    const cell = await openRoute(page, { path: '/agents', reducedMotion: 'reduce', mockOverrides: overridesFor() });
    try {
      await expect(cell.locator('[data-testid^="daemon-row-"]').first()).toBeVisible();
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
      expect(offenders, '/agents: durations > 0 under reduced motion').toEqual([]);
    } finally {
      await cell.close();
    }
  });

  for (const locale of LOCALES) {
    test(`390px ${locale}: controls sit inside the viewport`, async ({ page }) => {
      const cell = await openRoute(page, { path: '/agents', width: 390, locale, mockOverrides: overridesFor() });
      try {
        await assertRenderedLocale(cell, locale, `/agents 390 ${locale}:`);
        await expect(cell.locator('[data-testid^="daemon-row-"]').first()).toBeVisible();
        const controls = await cell.evaluate(tagPrimaryControls);
        expect(controls.length, '/agents: primary controls found').toBeGreaterThan(0);
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
        expect(outside, `/agents 390 ${locale}: controls outside the viewport`).toEqual([]);
      } finally {
        await cell.close();
      }
    });
  }

  for (const theme of THEMES) {
    test(`${theme}: square geometry, nothing above radius.card`, async ({ page }) => {
      const cell = await openRoute(page, { path: '/agents', theme, locale: 'en-US', mockOverrides: overridesFor() });
      try {
        await assertRenderedTheme(cell, theme, '/agents');
        await expect(cell.locator('[data-testid^="daemon-row-"]').first()).toBeVisible();
        const tooRound = await cell.evaluate(() => {
          const bad = [];
          for (const el of document.querySelectorAll('main.app-main *')) {
            const cs = getComputedStyle(el);
            for (const corner of ['borderTopLeftRadius', 'borderTopRightRadius', 'borderBottomLeftRadius', 'borderBottomRightRadius']) {
              const raw = cs[corner];
              if (!raw.endsWith('px')) continue;
              const v = parseFloat(raw);
              if (Number.isFinite(v) && v > 3.5) bad.push(`${el.tagName.toLowerCase()}.${el.className} ${corner}=${raw}`);
            }
          }
          return bad.slice(0, 20);
        });
        expect(tooRound, `/agents ${theme}: no border-radius above radius.card`).toEqual([]);
      } finally {
        await cell.close();
      }
    });
  }
});
