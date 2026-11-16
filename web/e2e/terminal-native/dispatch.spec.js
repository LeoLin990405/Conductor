/**
 * S17 acceptance for the terminal-native dispatch pages.
 *
 *  - /dispatch and /dispatch/:id are shot through the full theme × width ×
 *    locale matrix, the detail page on a dispatch that waits for a person;
 *  - Tab reaches every primary control of both routes and each wears the 2px
 *    ink.focus ring (SPEC.md: 2px solid, offset 2px, the theme's ink.focus);
 *  - under reducedMotion 'reduce' no element has a transition or animation
 *    duration above 0;
 *  - `?confirm=approve` opens the confirmation dialog and nothing else: no
 *    decision is taken from the query string alone.
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

const ROUTES = [
  { name: 'list', path: '/dispatch' },
  { name: 'detail', path: '/dispatch/7f3a91c2' },
];

/** tokens.json ink.focus per theme, as Chromium serializes it. */
const INK_FOCUS_RGB = {
  dark: 'rgb(139, 156, 255)', // #8B9CFF
  light: 'rgb(63, 79, 200)', // #3F4FC8
};

/** The active dispatches the list shows. */
const ACTIVE = [
  {
    id: '7f3a91c2',
    goal: 'Rebuild the dispatch list and the detail page',
    state: 'APPROVAL',
    progress_pct: 75,
    lines: [{ id: 'line-0' }],
  },
  {
    id: '4b8e0d17',
    goal: 'Keep the handoff when a native CLI worker rotates its context',
    state: 'REVIEW',
    progress_pct: 40,
    lines: [{ id: 'line-0' }, { id: 'line-1' }],
  },
];

/** The dispatch the detail page reads: waiting for a person. */
const DETAIL = {
  id: '7f3a91c2',
  goal: 'Rebuild the dispatch list and the detail page',
  state: 'APPROVAL',
  progress_pct: 75,
  integrate_branch: 'integrate/auto-dispatch-7f3a91c2',
  message: 'G5 passed. Awaiting human approval.',
  lines: [
    {
      id: 'line-0',
      name: 'autonomy-scheduler',
      status: 'PASSED',
      assigned_model: 'kimi-k3-coding',
      branch: 'line/autonomy-scheduler',
      base_commit: '9a2c6f30',
      depends_on: [],
    },
    {
      id: 'line-1',
      name: 'review-merge',
      status: 'REVIEWING',
      assigned_model: 'gpt-5.6-terra',
      branch: 'line/review-merge',
      base_commit: '3419e39e',
      depends_on: ['line-0'],
    },
    {
      id: 'line-2',
      name: 'handoff-rotation',
      status: 'BLOCKED',
      assigned_model: 'composer-1',
      branch: 'line/handoff-rotation',
      base_commit: '5d1e7a02',
      attempts: 2,
      depends_on: [],
    },
    {
      id: 'line-3',
      name: 'provider-roster',
      status: 'RUNNING',
      assigned_model: 'deepseek-v4-pro',
      branch: 'line/provider-roster',
      base_commit: '9a2c6f30',
      attempts: 1,
      depends_on: ['line-2'],
    },
  ],
  gates: {
    lines: {
      'line-0': {
        phase: 'awaiting_human',
        author: 'kimi-k3-coding',
        primary_reviewer: 'codex',
        gates: {
          G0: { result: 'pass', reason: 'planner_output_valid' },
          primary_review: { result: 'pass', verdict: 'merge', reviewer: 'codex', blocker_count: 0 },
          G6: { result: 'pending', reason: 'awaiting_human' },
        },
      },
      'line-1': {
        phase: 'secondary_review',
        author: 'gpt-5.6-terra',
        primary_reviewer: 'kimi-k3-coding',
        gates: {
          primary_review: { result: 'pass', verdict: 'merge', reviewer: 'kimi-k3-coding', blocker_count: 0 },
          secondary_review: { result: 'fail', verdict: 'block', reviewer: 'codex', blocker_count: 1 },
        },
      },
      'line-2': {
        phase: 'fixing',
        author: 'composer-1',
        primary_reviewer: 'codex',
        gates: { G2: { result: 'pass', revision: 'c41d09e7' } },
        history: [
          { round: 0, gate: 'primary_review', result: 'block', reviewer: 'codex', blocker_count: 3 },
          { round: 1, gate: 'primary_review', result: 'block', reviewer: 'codex', blocker_count: 1 },
        ],
        last_review_blockers: { head: 'e8b27f51', branch: 'line/handoff-rotation', count: 1 },
      },
      'line-3': {
        phase: 'implementing',
        author: 'deepseek-v4-pro',
      },
    },
    approval: { token_present: true },
  },
};

/** A dispatch that is not in the active list, so the history row is its own. */
const HISTORY_ID = 'c0ffee42';

const OVERRIDES = {
  'GET /features': { features: [] },
  'GET /autonomy/overview': {
    counts: { attention: 0, running: 0, queued: 0, completed: 0, failed: 0 },
    sources: 0,
    enabled_sources: 0,
  },
  'GET /autonomy/sources': { sources: [] },
  'GET /autonomy/tickets': { tickets: [] },
  'GET /native-agents': { runtimes: [] },
  'GET /dispatch/status/active': ACTIVE,
  // The generic pattern first and the named ones after it: Playwright answers
  // with the route that was registered last, so a named dispatch gets its
  // own fixture.
  'GET /dispatch/*': DETAIL,
  'GET /dispatch/7f3a91c2': DETAIL,
  'GET /dispatch/c0ffee42': {
    ...DETAIL,
    id: HISTORY_ID,
    state: 'COMPLETE',
    goal: 'A dispatch from the history',
    message: 'Merged and landed.',
  },
  'GET /models': {
    models: {
      kimi: { display_name: 'Kimi', models: ['kimi-k3-coding'] },
      openai: { display_name: 'OpenAI', models: ['gpt-5.6-terra'] },
      codex: { display_name: 'Codex', models: ['gpt-5.6-terra'] },
      cursor: { display_name: 'Cursor', models: ['composer-1'] },
      'deepseek-dsh': { display_name: 'DeepSeek', models: ['deepseek-v4-pro'] },
    },
    model_metadata: {
      'kimi-k3-coding': { provider: 'kimi' },
      'gpt-5.6-terra': { provider: 'openai' },
      codex: { provider: 'codex' },
    },
  },
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

test.describe('terminal-native dispatch', () => {
  test('shoots the list and an awaiting_human detail across the matrix', async ({ page }) => {
    test.setTimeout(300_000);
    for (const route of ROUTES) {
      const files = await shootMatrix(page, {
        slice: 'dispatch',
        name: route.name,
        path: route.path,
        mockOverrides: OVERRIDES,
      });
      expect(files, `${route.name}: 8 PNGs`).toHaveLength(8);
    }
  });

  test('the detail page shows L1-L4, each with its provider and model', async ({ page }) => {
    const cell = await openRoute(page, {
      path: '/dispatch/7f3a91c2',
      locale: 'en-US',
      mockOverrides: OVERRIDES,
    });
    try {
      const cards = cell.locator('article.tn-dispatch-line');
      await expect(cards).toHaveCount(4);
      const expected = [
        ['L1', 'kimi', 'kimi-k3-coding'],
        ['L2', 'openai', 'gpt-5.6-terra'],
        ['L3', 'cursor', 'composer-1'],
        ['L4', 'deepseek-dsh', 'deepseek-v4-pro'],
      ];
      for (const [index, [name, provider, model]] of expected.entries()) {
        const card = cards.nth(index);
        await expect(card.locator('.tn-dispatch-line-name')).toHaveText(name);
        const worker = card.locator('dd').first();
        await expect(worker.locator('.tn-dispatch-provider')).toHaveText(provider);
        await expect(worker.locator('.tn-dispatch-mono')).toHaveText(model);
      }
      // The review the line went through twice shows both rounds, and the
      // pinned commit is the head the review saw, not the branch base.
      const table = cell.locator('table.tn-dispatch-table');
      await expect(table.getByRole('row').filter({ hasText: 'line-2' })).toHaveCount(2);
      await expect(table.getByText('e8b27f51').first()).toBeVisible();
      await expect(table.getByText('5d1e7a02')).toHaveCount(0);
    } finally {
      await cell.close();
    }
  });

  test('shoots the list with a populated history across the matrix', async ({ page }) => {
    test.setTimeout(300_000);
    const files = await shootMatrix(page, {
      slice: 'dispatch',
      name: 'list-history',
      path: '/dispatch',
      mockOverrides: OVERRIDES,
      prepare: async (cell) => {
        // The history is the dispatches this user has seen or started. They
        // live in the user's browser storage, scoped by the signed-in user
        // (lib/userStorage); the e2e app runs in OSS mode, so the user is the
        // local one. Seeded before the app boots, so the list reads it on
        // its first render.
        await cell.evaluate((ids) => {
          localStorage.setItem('user:local-dev-user:dispatch.history', JSON.stringify(ids));
        }, [HISTORY_ID]);
        await cell.reload({ waitUntil: 'domcontentloaded' });
        // The heading carries the id; the section it names carries the rows.
        const history = cell.locator('section[aria-labelledby="tn-dispatch-history"]');
        await expect(history).toBeVisible();
        await expect(history.getByText(HISTORY_ID)).toBeVisible();
        // The remembered dispatch shows the state it is in now: a glyph and
        // the word for the language the page is in.
        const lang = await cell.evaluate(() => document.documentElement.lang);
        await expect(history.getByText('✔')).toBeVisible();
        await expect(history.getByText(lang.startsWith('zh') ? '已完成' : 'completed')).toBeVisible();
      },
    });
    expect(files, 'list-history: 8 PNGs').toHaveLength(8);
  });

  for (const route of ROUTES) {
    for (const theme of THEMES) {
      test(`${route.path} ${theme}: Tab reaches every primary control and each wears the 2px ink.focus ring`, async ({
        page,
      }) => {
        test.setTimeout(90_000);
        const cell = await openRoute(page, {
          path: route.path,
          theme,
          locale: 'en-US',
          mockOverrides: OVERRIDES,
        });
        try {
          await assertRenderedTheme(cell, theme, route.path);

          const focusRgb = await cell.evaluate(resolveInkFocus);
          expect(focusRgb, `${route.path} ${theme}: --ink-focus on :root`).toBe(
            INK_FOCUS_RGB[theme],
          );

          const controls = await cell.evaluate(tagPrimaryControls);
          expect(controls.length, `${route.path}: primary controls found`).toBeGreaterThan(0);

          await cell.evaluate(() => {
            document.activeElement?.blur();
            window.scrollTo(0, 0);
          });
          const reached = new Map();
          const MAX = 250;
          for (let i = 0; i < MAX; i += 1) {
            await cell.keyboard.press('Tab');
            const ring = await cell.evaluate(focusedRing);
            if (ring.seen || (ring.body && i > 0)) break; // wrapped
            if (ring.ctl == null) continue; // a shell control outside the route
            reached.set(ring.ctl, ring);
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
      test(`${route.path} 390px ${locale}: no horizontal scroll and every control inside the viewport`, async ({
        page,
      }) => {
        const cell = await openRoute(page, {
          path: route.path,
          width: 390,
          locale,
          mockOverrides: OVERRIDES,
        });
        try {
          await assertRenderedLocale(cell, locale, `${route.path} 390 ${locale}:`);
          const controls = await cell.evaluate(tagPrimaryControls);
          expect(controls.length, `${route.path}: primary controls found`).toBeGreaterThan(0);

          const m = await cell.evaluate(() => {
            const se = document.scrollingElement;
            const main = document.querySelector('main.app-main');
            const vw = document.documentElement.clientWidth;
            const outside = [];
            for (const el of document.querySelectorAll('[data-tn-ctl]')) {
              const r = el.getBoundingClientRect();
              if (r.left >= -0.5 && r.right <= vw + 0.5) continue;
              const name = (el.getAttribute('aria-label') || el.textContent || '')
                .trim()
                .slice(0, 30);
              outside.push(`${el.tagName.toLowerCase()} "${name}" left ${r.left.toFixed(1)} right ${r.right.toFixed(1)}`);
            }
            return {
              page: { scrollWidth: se.scrollWidth, clientWidth: se.clientWidth },
              main: main
                ? { scrollWidth: main.scrollWidth, clientWidth: main.clientWidth }
                : null,
              outside,
            };
          });

          const where = `${route.path} 390 ${locale}`;
          expect(
            m.page.scrollWidth,
            `${where}: page scrolls sideways ${JSON.stringify(m.page)}`,
          ).toBeLessThanOrEqual(m.page.clientWidth);
          expect(m.main, `${where}: main.app-main`).toBeTruthy();
          expect(
            m.main.scrollWidth,
            `${where}: main.app-main overflows ${JSON.stringify(m.main)}`,
          ).toBeLessThanOrEqual(m.main.clientWidth);
          expect(m.outside, `${where}: controls outside the viewport`).toEqual([]);
        } finally {
          await cell.close();
        }
      });
    }
  }

  test("reducedMotion 'reduce' leaves no transition or animation duration > 0", async ({ page }) => {
    for (const route of ROUTES) {
      const cell = await openRoute(page, {
        path: route.path,
        reducedMotion: 'reduce',
        mockOverrides: OVERRIDES,
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

  test('a long zh-CN goal and a long model name fit at 390', async ({ page }) => {
    const longGoal =
      '把派发的列表页和详情页重做成终端原生风格，并且让每一条线都带上它的 provider 与模型名';
    const cell = await openRoute(page, {
      path: '/dispatch',
      width: 390,
      locale: 'zh-CN',
      mockOverrides: {
        ...OVERRIDES,
        'GET /dispatch/status/active': [
          {
            id: '7f3a91c2',
            goal: longGoal,
            state: 'APPROVAL',
            progress_pct: 75,
            lines: [{ id: 'line-0' }],
          },
        ],
      },
    });
    try {
      await expect(cell.getByText(longGoal)).toBeVisible();
      await assertFitsAt390(cell, 'zh-CN 390 list');
    } finally {
      await cell.close();
    }
  });

  for (const long of [
    {
      name: 'English',
      locale: 'en-US',
      model: 'gpt-5.6-terra-super-long-model-name-for-a-provider-with-a-long-name',
      note: 'The rehearsal ran on a throwaway worktree and the merge landed only the commit that passed.',
    },
    {
      name: 'Chinese',
      locale: 'zh-CN',
      model: '原生代码工作流运行时的超长模型名称测试用例',
      note: '彩排在一次性 worktree 中重跑，落地的是彩排通过的那一个提交，其余的都被退回修复中。',
    },
  ]) {
    test(`a long ${long.name} model name and note fit the detail page at 390`, async ({ page }) => {
      const cell = await openRoute(page, {
        path: '/dispatch/7f3a91c2',
        width: 390,
        locale: long.locale,
        mockOverrides: {
          ...OVERRIDES,
          'GET /dispatch/7f3a91c2': {
            ...DETAIL,
            message: long.note,
            lines: [
              {
                ...DETAIL.lines[0],
                assigned_model: long.model,
                branch: `line/${long.model}`,
              },
              ...DETAIL.lines.slice(1),
            ],
          },
        },
      });
      try {
        // The long model name and the long note are both on the page, and
        // neither pushes the layout sideways. The model name is also in the
        // branch, so the match is exact.
        await expect(cell.getByText(long.model, { exact: true })).toBeVisible();
        await expect(cell.getByText(long.note)).toBeVisible();
        await assertFitsAt390(cell, `${long.name} 390 detail`);
      } finally {
        await cell.close();
      }
    });
  }

  test('?confirm=approve opens the confirmation dialog and decides nothing until it is confirmed', async ({
    page,
  }) => {
    const decisions = [];
    const cell = await openRoute(page, {
      path: '/dispatch/7f3a91c2?confirm=approve',
      locale: 'en-US',
      mockOverrides: OVERRIDES,
    });
    try {
      // Watch the decision route: the query string may open the dialog, but
      // only the confirmation may reach the backend.
      await cell.route('**/dispatch/*/approve', async (route) => {
        decisions.push(route.request().postDataJSON());
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ id: '7f3a91c2', state: 'MERGE' }),
        });
      });

      const dialog = cell.getByRole('dialog');
      await expect(dialog).toBeVisible();
      await expect(dialog.getByRole('button', { name: 'Approve' })).toBeVisible();
      expect(decisions, 'nothing decided before the confirmation').toEqual([]);

      await dialog.getByRole('button', { name: 'Approve' }).click();
      await expect.poll(() => decisions.length).toBe(1);
      expect(decisions[0]).toEqual({ note: '' });
    } finally {
      await cell.close();
    }
  });
});
