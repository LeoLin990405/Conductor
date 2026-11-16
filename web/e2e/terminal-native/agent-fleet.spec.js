/**
 * S22 acceptance for the terminal-native agent fleet page (/agent-fleet).
 *
 *  - /agent-fleet is shot through the full theme × width × locale matrix,
 *    once with the roster only and once with an agent's log panel open;
 *  - the provider roster lists exactly the providers in the scan (data, not
 *    copy), each with a health glyph and word;
 *  - Tab reaches every primary control and each wears the 2px ink.focus ring;
 *  - at 390 neither the page nor main.app-main scrolls sideways, in en-US and
 *    zh-CN, with long provider, model and branch names;
 *  - under reducedMotion 'reduce' no element has a transition or animation
 *    duration above 0 (the running glyph does not breathe).
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

const PATH = '/agent-fleet';

/** tokens.json ink.focus per theme, as Chromium serializes it. */
const INK_FOCUS_RGB = {
  dark: 'rgb(139, 156, 255)', // #8B9CFF
  light: 'rgb(63, 79, 200)', // #3F4FC8
};

/** The fleet roster: these names are fixture data, never product copy. */
const ROSTER = [
  ['codex', 'gpt-5.6-terra', 'running', 'ok', 'dev'],
  ['kimi', 'kimi-k3-coding', 'running', 'ok', 'dev'],
  ['cursor', 'composer-1', 'stalled', 'unknown', 'review'],
  ['grok', 'grok-5-code', 'done', 'ok', 'research'],
  ['minimax', 'minimax-m3', 'error', 'low', 'dev'],
  ['deepseek-dsh', 'deepseek-v4-pro', 'running', 'ok', 'plan'],
  ['zcode', 'glm-5.2', 'unknown', 'unknown', 'review'],
  ['stepfun', 'step-3.7-flash', 'running', 'low', 'dev'],
];

function agentRow([provider, model, status, balance, role], index) {
  const id = `w-${provider}`;
  return {
    id,
    model,
    provider,
    role,
    pid: status === 'running' ? 4100 + index : null,
    worktree: `/Users/dev/conductor-wt/${id}`,
    branch: `wave/f5-${provider}`,
    status,
    elapsed_secs: status === 'unknown' ? null : 600 + index * 731,
    last_activity_ts: '2026-11-14T06:00:00Z',
    log_file: `${id}.log`,
    log_tail: `step ${index}: running the focused tests\nAuthorization: [REDACTED]`,
    commits_ahead: status === 'unknown' ? null : index,
    tests_state: status === 'done' ? '42 passed' : null,
    provider_balance: balance,
  };
}

const FLEET = {
  agents: ROSTER.map(agentRow),
  scan_ts: '2026-11-14T06:00:00Z',
  stall_threshold_mins: 5,
  error: null,
};

const LOGS = {
  agent_id: 'w-codex',
  log_file: 'w-codex.log',
  lines: ['starting work', 'Authorization: [REDACTED]', 'ran 12 tests, 12 passed'],
  truncated: true,
};

const OVERRIDES = {
  'GET /agents/fleet': FLEET,
  'GET /agents/*/logs': LOGS,
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

/**
 * The page does not scroll sideways and every control sits inside the
 * viewport. Runs in the page.
 */
async function assertFitsAt390(cell, label) {
  await cell.evaluate(tagPrimaryControls);
  const scroll = await cell.evaluate(() => {
    const se = document.scrollingElement;
    const main = document.querySelector('main.app-main');
    const vw = document.documentElement.clientWidth;
    const outside = [];
    for (const el of document.querySelectorAll('[data-tn-ctl]')) {
      const r = el.getBoundingClientRect();
      if (r.left >= -0.5 && r.right <= vw + 0.5) continue;
      const name = (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 30);
      outside.push(`${el.tagName.toLowerCase()} "${name}" left ${r.left.toFixed(1)} right ${r.right.toFixed(1)}`);
    }
    return {
      page: { scrollWidth: se.scrollWidth, clientWidth: se.clientWidth },
      main: main ? { scrollWidth: main.scrollWidth, clientWidth: main.clientWidth } : null,
      outside,
    };
  });
  expect(scroll.page.scrollWidth, `${label}: page scrolls sideways ${JSON.stringify(scroll.page)}`).toBeLessThanOrEqual(
    scroll.page.clientWidth,
  );
  expect(
    scroll.main.scrollWidth,
    `${label}: main.app-main overflows ${JSON.stringify(scroll.main)}`,
  ).toBeLessThanOrEqual(scroll.main.clientWidth);
  expect(scroll.outside, `${label}: controls outside the viewport`).toEqual([]);
}

/** Run the Tab walk and return the ring each primary control wore. */
async function walkTab(cell) {
  await cell.evaluate(() => {
    document.activeElement?.blur();
    window.scrollTo(0, 0);
  });
  const reached = new Map();
  for (let i = 0; i < 250; i += 1) {
    await cell.keyboard.press('Tab');
    const ring = await cell.evaluate(focusedRing);
    if (ring.seen || (ring.body && i > 0)) break; // wrapped
    if (ring.ctl == null) continue; // a shell control outside the route
    reached.set(ring.ctl, ring);
  }
  return reached;
}

test.describe('terminal-native agent fleet', () => {
  test('shoots /agent-fleet across the matrix', async ({ page }) => {
    test.setTimeout(300_000);
    const files = await shootMatrix(page, {
      slice: 'agent-fleet',
      name: 'fleet',
      path: PATH,
      mockOverrides: OVERRIDES,
      prepare: async (cell) => {
        await expect(cell.locator('.tn-fleet-roster-row')).toHaveCount(ROSTER.length);
      },
    });
    expect(files, 'fleet: 8 PNGs').toHaveLength(8);
  });

  test('shoots /agent-fleet with a log panel open across the matrix', async ({ page }) => {
    test.setTimeout(300_000);
    const files = await shootMatrix(page, {
      slice: 'agent-fleet',
      name: 'logs',
      path: PATH,
      mockOverrides: OVERRIDES,
      prepare: async (cell) => {
        await cell.getByRole('button', { name: 'w-codex', exact: true }).click();
        await expect(cell.locator('.tn-fleet-logs pre')).toContainText('[REDACTED]');
      },
    });
    expect(files, 'logs: 8 PNGs').toHaveLength(8);
  });

  for (const locale of LOCALES) {
    test(`${locale}: the roster is the providers in the scan, each with a health glyph and word`, async ({
      page,
    }) => {
      const cell = await openRoute(page, { path: PATH, locale, mockOverrides: OVERRIDES });
      try {
        const rows = cell.locator('.tn-fleet-roster-row');
        await expect(rows).toHaveCount(ROSTER.length);
        const providers = await rows.evaluateAll((els) => els.map((el) => el.dataset.provider));
        expect(providers).toEqual(ROSTER.map(([p]) => p).sort((a, b) => a.localeCompare(b)));
        const words = locale === 'zh-CN'
          ? { ok: '充足', low: '偏低', unknown: '未知' }
          : { ok: 'ok', low: 'low', unknown: 'unknown' };
        const glyphs = { ok: '✔', low: '◆', unknown: '○' };
        for (const [provider, , , balance] of ROSTER) {
          const health = cell.locator(`.tn-fleet-roster-row[data-provider="${provider}"] .tn-fleet-mark`);
          await expect(health.locator('.tn-fleet-glyph')).toHaveText(glyphs[balance]);
          await expect(health.locator('.tn-fleet-word')).toHaveText(words[balance]);
        }
        // Every agent row has its status as a glyph and a word.
        await expect(cell.locator('table.tn-fleet-table tbody tr')).toHaveCount(ROSTER.length);
        await expect(cell.locator('table.tn-fleet-table .tn-fleet-status .tn-fleet-word')).toHaveCount(
          ROSTER.length,
        );
      } finally {
        await cell.close();
      }
    });
  }

  test('the roster provider button filters the agents from the keyboard', async ({ page }) => {
    const cell = await openRoute(page, { path: PATH, mockOverrides: OVERRIDES });
    try {
      const kimi = cell.locator('.tn-fleet-roster-row[data-provider="kimi"] button');
      await kimi.focus();
      await cell.keyboard.press('Enter');
      await expect(kimi).toHaveAttribute('aria-pressed', 'true');
      await expect(cell.locator('table.tn-fleet-table tbody tr')).toHaveCount(1);
      await cell.keyboard.press('Space');
      await expect(cell.locator('table.tn-fleet-table tbody tr')).toHaveCount(ROSTER.length);
    } finally {
      await cell.close();
    }
  });

  for (const theme of THEMES) {
    test(`${theme}: Tab reaches every primary control and each wears the 2px ink.focus ring`, async ({
      page,
    }) => {
      test.setTimeout(90_000);
      const cell = await openRoute(page, {
        path: PATH,
        theme,
        locale: 'en-US',
        mockOverrides: OVERRIDES,
      });
      try {
        await assertRenderedTheme(cell, theme, PATH);
        // Open a log panel so its Close button is a primary control too.
        await cell.getByRole('button', { name: 'w-codex', exact: true }).click();
        await expect(cell.locator('.tn-fleet-logs pre')).toBeVisible();
        // A click moves Chromium's sequential-focus starting point to the
        // clicked node, so Tab would resume after w-codex and never visit the
        // filters above it. Click the (non-focusable) page heading to put the
        // starting point back at the top of the route before the walk.
        await cell.locator('.tn-fleet-head h1').click();

        const focusRgb = await cell.evaluate(resolveInkFocus);
        expect(focusRgb, `${theme}: --ink-focus on :root`).toBe(INK_FOCUS_RGB[theme]);

        const controls = await cell.evaluate(tagPrimaryControls);
        // 5 filters + 8 roster providers + 8 agent buttons + Close.
        expect(controls.length, 'primary controls found').toBeGreaterThanOrEqual(22);

        const reached = await walkTab(cell);
        const unreached = controls.filter((c) => !reached.has(String(c.i))).map((c) => c.desc);
        expect(unreached, `${theme}: controls Tab never reached`).toEqual([]);

        const bad = [];
        for (const c of controls) {
          const problem = ringProblem(reached.get(String(c.i)), focusRgb);
          if (problem) bad.push(`${c.desc}: ${problem}`);
        }
        expect(bad, `${theme}: controls without the ink.focus ring`).toEqual([]);
      } finally {
        await cell.close();
      }
    });
  }

  for (const locale of LOCALES) {
    test(`390px ${locale}: no horizontal scroll and every control inside the viewport`, async ({
      page,
    }) => {
      const cell = await openRoute(page, {
        path: PATH,
        width: 390,
        locale,
        mockOverrides: OVERRIDES,
      });
      try {
        await assertRenderedLocale(cell, locale, `${PATH} 390 ${locale}:`);
        await cell.getByRole('button', { name: 'w-codex', exact: true }).click();
        await expect(cell.locator('.tn-fleet-logs pre')).toBeVisible();
        await assertFitsAt390(cell, `${PATH} 390 ${locale}`);
      } finally {
        await cell.close();
      }
    });
  }

  for (const long of [
    {
      name: 'English',
      locale: 'en-US',
      provider: 'deepseek-dsh-provider-with-a-very-long-registry-name',
      model: 'gpt-5.6-terra-super-long-model-name-for-a-provider-with-a-long-name',
    },
    {
      name: 'Chinese',
      locale: 'zh-CN',
      provider: '超长供应商名称测试用例供应商',
      model: '原生代码工作流运行时的超长模型名称测试用例',
    },
  ]) {
    test(`a long ${long.name} provider and model fit at 390`, async ({ page }) => {
      const agents = FLEET.agents.map((agent, index) =>
        index === 0
          ? { ...agent, provider: long.provider, model: long.model, branch: `wave/${long.model}` }
          : agent,
      );
      const cell = await openRoute(page, {
        path: PATH,
        width: 390,
        locale: long.locale,
        mockOverrides: { ...OVERRIDES, 'GET /agents/fleet': { ...FLEET, agents } },
      });
      try {
        await expect(cell.getByRole('button', { name: long.provider, exact: true })).toBeVisible();
        await expect(cell.getByText(long.model, { exact: true }).first()).toBeVisible();
        await assertFitsAt390(cell, `${long.name} 390 fleet`);
      } finally {
        await cell.close();
      }
    });
  }

  test("reducedMotion 'reduce' leaves no transition or animation duration > 0", async ({ page }) => {
    const cell = await openRoute(page, {
      path: PATH,
      reducedMotion: 'reduce',
      mockOverrides: OVERRIDES,
    });
    try {
      await expect(cell.locator('.tn-fleet-status--running').first()).toBeVisible();
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
      expect(offenders, `${PATH}: durations > 0 under reduced motion`).toEqual([]);
      // The running glyph keeps ● and does not breathe.
      const glyph = cell.locator('.tn-fleet-status--running .tn-fleet-glyph').first();
      await expect(glyph).toHaveText('●');
      expect(await glyph.evaluate((el) => getComputedStyle(el).animationName)).toBe('none');
    } finally {
      await cell.close();
    }
  });
});
