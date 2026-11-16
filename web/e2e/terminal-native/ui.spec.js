/**
 * S13 acceptance for the terminal-native UI primitives.
 *
 * `shootMatrix` walks {dark,light} x {1440,390} x {en-US,zh-CN} twice on
 * Settings > Preferences: once with the reset-preferences ConfirmDialog open,
 * once with the toast the app raises when the preference write is refused.
 * Separate cells prove that Tab and Shift+Tab stay trapped in the dialog and
 * every control wears the 2px ink.focus ring, that Esc closes it and returns
 * focus to the opener, that the toast's close control wears the same ring,
 * that long en / zh-CN footer labels wrap inside their buttons (390 and 1440),
 * that under reducedMotion 'reduce' no computed transition or animation
 * duration is > 0, and that dialog and toast wear the one floating fill
 * (surface elevation 1, the elevation.1 shadow, a rule-ink hairline, radii
 * capped at radius.overlay).
 *
 * Labels are read from the main catalogs (read-only; S30 owns them), so the
 * spec finds its controls in whichever language the cell booted in.
 */
import { readFileSync } from 'node:fs';

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

const SETTINGS = '/settings?tab=preferences';
const CATALOGS = Object.fromEntries(
  LOCALES.map((locale) => [
    locale,
    JSON.parse(readFileSync(new URL(`../../src/locales/${locale}.json`, import.meta.url), 'utf8')),
  ]),
);

function t(locale, key) {
  const value = key
    .split('.')
    .reduce((node, part) => (node == null ? undefined : node[part]), CATALOGS[locale]);
  if (typeof value !== 'string') throw new Error(`the ${locale} catalog has no ${key}`);
  return value;
}

// The preference write is refused here so the toast is the app's own failure
// notice. mockAPI fulfills it; nothing is sent to a network.
const MOCKS = {
  'PUT /users/me/preferences': (route) =>
    route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'e2e: the preference write is refused' }),
    }),
};

// The shipped footer labels fit on one line, so the wrap contract is measured
// against a longer label in the language the cell booted in. Each carries one
// unbroken token (a 64-hex snapshot id, no hyphen or slash to break at) wider
// than a button can be, so `break-words` (not only the spaces) is what keeps
// it inside.
const LONG_FOOTER_LABEL = {
  'en-US':
    'Restore every preference saved on this device and clear all customizations in snapshot 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08',
  'zh-CN':
    '恢复此设备上保存的全部偏好设置并清除所有已保存的自定义内容 snapshot 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08',
};

async function settle(cell) {
  await cell.evaluate(async () => {
    await Promise.race([
      Promise.all(
        document
          .getAnimations()
          .filter((animation) => animation.effect?.getComputedTiming().endTime !== Infinity)
          .map((animation) => animation.finished.catch(() => {})),
      ),
      new Promise((resolve) => setTimeout(resolve, 1000)),
    ]);
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}

async function openConfirmDialog(cell) {
  const locale = await cell.evaluate(() => document.documentElement.lang);
  const opener = cell
    .locator('main.app-main')
    .getByRole('button', { name: t(locale, 'settings.resetPreferences'), exact: true });
  await expect(opener).toBeVisible();
  await opener.evaluate((element) => element.setAttribute('data-tn-opener', ''));
  await opener.click();

  const dialog = cell.getByRole('dialog');
  await expect(dialog).toBeVisible({ timeout: 10_000 });
  await settle(cell);
  const bounds = await dialog.boundingBox();
  expect(bounds).toBeTruthy();
  expect(bounds.x).toBeGreaterThanOrEqual(-0.5);
  expect(bounds.y).toBeGreaterThanOrEqual(-0.5);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(cell.viewportSize().width + 0.5);
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(cell.viewportSize().height + 0.5);
  return dialog;
}

async function raiseToast(cell) {
  const locale = await cell.evaluate(() => document.documentElement.lang);
  await cell
    .getByRole('button', { name: t(locale, 'settings.outputFormatHtml'), exact: true })
    .click();
  const toast = cell.locator('.toast-slide');
  await expect(toast).toBeVisible({ timeout: 10_000 });
  await settle(cell);
  const bounds = await toast.boundingBox();
  expect(bounds).toBeTruthy();
  expect(bounds.x).toBeGreaterThanOrEqual(-0.5);
  expect(bounds.y).toBeGreaterThanOrEqual(-0.5);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(cell.viewportSize().width + 0.5);
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(cell.viewportSize().height + 0.5);
  return toast;
}

function tagDialogControls() {
  const dialog = document.querySelector('[role="dialog"]');
  if (!dialog) throw new Error('no dialog on the page');
  const controls = 'a[href], button, input:not([type="hidden"]), select, textarea';
  const candidates = `${controls}, [tabindex]`;
  const tagged = [];

  for (const element of dialog.querySelectorAll(candidates)) {
    if (!element.matches(controls) && element.tabIndex < 0) continue;
    if (element.matches(':disabled') || element.getAttribute('aria-disabled') === 'true') continue;
    if (element.closest('[inert], [aria-hidden="true"]')) continue;
    const style = getComputedStyle(element);
    if (!element.getClientRects().length || style.visibility === 'hidden' || style.display === 'none') {
      continue;
    }
    const index = tagged.length;
    element.setAttribute('data-tn-ctl', String(index));
    const name = (element.getAttribute('aria-label') || element.textContent || '')
      .trim()
      .replace(/\s+/g, ' ')
      .slice(0, 40);
    tagged.push({ index, description: `${element.tagName.toLowerCase()} "${name}"` });
  }
  return tagged;
}

function focusedStop() {
  const element = document.activeElement;
  if (!element || element === document.body || element === document.documentElement) {
    return { control: null, inside: false };
  }
  const style = getComputedStyle(element);
  return {
    control: element.getAttribute('data-tn-ctl'),
    inside: Boolean(element.closest('[role="dialog"]')),
    outlineStyle: style.outlineStyle,
    outlineWidth: style.outlineWidth,
    outlineColor: style.outlineColor,
    outlineOffset: style.outlineOffset,
  };
}

function resolveInkFocus() {
  if (!getComputedStyle(document.documentElement).getPropertyValue('--ink-focus').trim()) {
    return null;
  }
  const probe = document.createElement('div');
  probe.style.outlineColor = 'var(--ink-focus)';
  document.body.append(probe);
  const color = getComputedStyle(probe).outlineColor;
  probe.remove();
  return color;
}

function outlineProblem(stop, focusColor) {
  const visible =
    stop.outlineStyle === 'solid' &&
    stop.outlineWidth === '2px' &&
    stop.outlineColor === focusColor &&
    stop.outlineColor !== 'transparent' &&
    !/,\s*0\)\s*$/.test(stop.outlineColor) &&
    ['2px', '-2px'].includes(stop.outlineOffset);
  return visible
    ? ''
    : `${stop.outlineStyle} ${stop.outlineWidth} ${stop.outlineColor} offset ${stop.outlineOffset}; ` +
        `want a visible 2px solid ${focusColor} outline`;
}

function applyLongFooterLabel({ label, cancelLabel, confirmLabel }) {
  const dialog = document.querySelector('[role="dialog"]');
  if (!dialog) throw new Error('no dialog on the page');
  const buttons = [...dialog.querySelectorAll('button')];
  const cancel = buttons.find((button) => button.textContent.trim() === cancelLabel);
  const confirm = buttons.find((button) => button.textContent.trim() === confirmLabel);
  if (!cancel || !confirm) throw new Error('the footer buttons do not match the catalog');
  const footer = cancel.parentElement;
  if (footer !== confirm.parentElement) throw new Error('the footer buttons share no parent');

  cancel.textContent = label;
  confirm.textContent = label;

  const measureButton = (button) => {
    const style = getComputedStyle(button);
    const rect = button.getBoundingClientRect();
    const range = document.createRange();
    range.selectNodeContents(button);
    const lines = [...range.getClientRects()]
      .filter((line) => line.width > 0.5 && line.height > 0.5)
      .map((line) => ({
        left: line.left,
        right: line.right,
        top: line.top,
        bottom: line.bottom,
      }));
    return {
      lines,
      content: {
        left: rect.left + (Number.parseFloat(style.paddingLeft) || 0),
        right: rect.right - (Number.parseFloat(style.paddingRight) || 0),
        bottom: rect.bottom - (Number.parseFloat(style.paddingBottom) || 0),
      },
      scrollWidth: button.scrollWidth,
      clientWidth: button.clientWidth,
    };
  };
  const dialogRect = dialog.getBoundingClientRect();
  return {
    viewportWidth: document.documentElement.clientWidth,
    dialog: { left: dialogRect.left, right: dialogRect.right },
    footer: { scrollWidth: footer.scrollWidth, clientWidth: footer.clientWidth },
    buttons: [measureButton(cancel), measureButton(confirm)],
  };
}

function motionOffenders() {
  const durations = (value) =>
    value.split(',').map((part) => {
      const raw = part.trim();
      if (!raw || raw === 'normal' || raw === 'auto') return 0;
      const number = Number.parseFloat(raw);
      if (!Number.isFinite(number)) return 0;
      return raw.endsWith('ms') ? number / 1000 : number;
    });
  const offenders = [];
  for (const element of document.querySelectorAll('*')) {
    for (const pseudo of [null, '::before', '::after']) {
      const style = getComputedStyle(element, pseudo);
      if (
        [...durations(style.transitionDuration), ...durations(style.animationDuration)].some(
          (duration) => duration > 0,
        )
      ) {
        const id = element.id ? `#${element.id}` : '';
        const classes = element.classList?.length ? `.${[...element.classList].join('.')}` : '';
        offenders.push(
          `${element.tagName.toLowerCase()}${id}${classes}${pseudo ?? ''} ` +
            `transition ${style.transitionDuration} animation ${style.animationDuration}`,
        );
      }
    }
  }
  return offenders.slice(0, 20);
}

function resolveToken({ token, property }) {
  const declared = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
  if (!declared) return null;
  const probe = document.createElement('div');
  probe.style[property] = `var(${token})`;
  document.body.append(probe);
  const value = getComputedStyle(probe)[property];
  probe.remove();
  return value;
}

function shadowLayers(value) {
  const layer =
    /(rgba?\([^)]*\)|hsla?\([^)]*\)|color\([^)]*\)|[a-z]+)\s+(-?[\d.]+px)\s+(-?[\d.]+px)\s+(-?[\d.]+px)(?:\s+(-?[\d.]+px))?/g;
  return [...value.matchAll(layer)].map((match) => ({
    color: match[1],
    offsetX: match[2],
    offsetY: match[3],
    blur: match[4],
    spread: match[5] ?? '0px',
  }));
}

function sameShadowLayer(left, right) {
  return (
    left &&
    right &&
    left.color === right.color &&
    left.offsetX === right.offsetX &&
    left.offsetY === right.offsetY &&
    left.blur === right.blur &&
    left.spread === right.spread
  );
}

function surfaceGeometry(selector) {
  const element = document.querySelector(selector);
  if (!element) throw new Error(`no ${selector} on the page`);
  const style = getComputedStyle(element);
  return {
    className: String(element.className || ''),
    backgroundColor: style.backgroundColor,
    boxShadow: style.boxShadow,
    borderColor: style.borderTopColor,
    borderWidth: style.borderTopWidth,
    radii: [
      style.borderTopLeftRadius,
      style.borderTopRightRadius,
      style.borderBottomRightRadius,
      style.borderBottomLeftRadius,
    ],
  };
}

function geometryProblems(geometry, contract) {
  const problems = [];
  if (geometry.backgroundColor !== contract.elevated) {
    problems.push(
      `fill ${geometry.backgroundColor} is not surface elevation 1 (${contract.elevated})`,
    );
  }
  if (
    !shadowLayers(geometry.boxShadow).some((layer) =>
      sameShadowLayer(layer, contract.shadowLayer),
    )
  ) {
    problems.push(`shadow ${geometry.boxShadow} is not the elevation.1 token shadow`);
  }
  if (geometry.borderWidth !== '1px') {
    problems.push(`border ${geometry.borderWidth} is not a 1px hairline`);
  }
  if (!contract.inks.includes(geometry.borderColor)) {
    problems.push(`border ${geometry.borderColor} is not an allowed rule ink`);
  }
  for (const radius of geometry.radii) {
    if (Number.parseFloat(radius) > contract.radiusCap) {
      problems.push(`radius ${radius} exceeds radius.overlay (${contract.radiusCap}px)`);
    }
  }
  return problems;
}

async function floatingContract(cell) {
  const [elevated, shadow, ruleInks, lossInks, radius] = await Promise.all([
    cell.evaluate(resolveToken, { token: '--surface-elevated', property: 'backgroundColor' }),
    cell.evaluate(resolveToken, { token: '--elevation-1', property: 'boxShadow' }),
    Promise.all(
      ['--ink-rule', '--color-border-muted'].map((token) =>
        cell.evaluate(resolveToken, { token, property: 'borderTopColor' }),
      ),
    ),
    Promise.all(
      ['--ink-error', '--color-border-loss'].map((token) =>
        cell.evaluate(resolveToken, { token, property: 'borderTopColor' }),
      ),
    ),
    cell.evaluate(resolveToken, { token: '--radius-overlay', property: 'borderTopLeftRadius' }),
  ]);
  return {
    base: {
      elevated,
      shadowLayer: shadowLayers(shadow)[0] ?? null,
      radiusCap: Number.parseFloat(radius),
    },
    ruleInks,
    lossInks,
  };
}

// Radix puts the toast viewport on the F8 hotkey; Tab from there reaches the
// controls inside the toast. Returns the focus state of the close control.
async function focusToastClose(cell) {
  await cell.keyboard.press('F8');
  for (let index = 0; index < 6; index += 1) {
    await cell.keyboard.press('Tab');
    const state = await cell.evaluate(() => {
      const element = document.activeElement;
      if (!(element instanceof HTMLElement) || !element.hasAttribute('toast-close')) return null;
      const style = getComputedStyle(element);
      return {
        outlineStyle: style.outlineStyle,
        outlineWidth: style.outlineWidth,
        outlineColor: style.outlineColor,
        outlineOffset: style.outlineOffset,
      };
    });
    if (state) return state;
  }
  return null;
}

test.describe('terminal-native UI primitives', () => {
  test('matrix: Settings confirm dialog and toast', async ({ page }) => {
    test.setTimeout(360_000);

    const dialogFiles = await shootMatrix(page, {
      slice: 'ui',
      name: 'confirm-dialog',
      path: SETTINGS,
      mockOverrides: MOCKS,
      prepare: openConfirmDialog,
    });
    expect(dialogFiles).toHaveLength(8);

    const toastFiles = await shootMatrix(page, {
      slice: 'ui',
      name: 'toast',
      path: SETTINGS,
      mockOverrides: MOCKS,
      prepare: raiseToast,
    });
    expect(toastFiles).toHaveLength(8);
  });

  for (const width of WIDTHS) {
    for (const locale of LOCALES) {
      test(`${width}px ${locale}: dialog focus trap, ink.focus outlines, and Esc focus return`, async ({
        page,
      }) => {
        test.setTimeout(90_000);
        const cell = await openRoute(page, { path: SETTINGS, width, locale, mockOverrides: MOCKS });
        try {
          await assertRenderedLocale(cell, locale);
          await openConfirmDialog(cell);
          const focusColor = await cell.evaluate(resolveInkFocus);
          expect(focusColor, 'the --ink-focus token resolves').not.toBeNull();
          const controls = await cell.evaluate(tagDialogControls);
          expect(controls.length, 'the dialog has more than one control').toBeGreaterThan(1);

          // One full lap forward plus one, then one lap back plus one: both
          // directions must wrap inside the dialog, never reach the page.
          const forward = [];
          for (let index = 0; index <= controls.length; index += 1) {
            await cell.keyboard.press('Tab');
            forward.push(await cell.evaluate(focusedStop));
          }
          const backward = [];
          for (let index = 0; index <= controls.length; index += 1) {
            await cell.keyboard.press('Shift+Tab');
            backward.push(await cell.evaluate(focusedStop));
          }
          const stops = [...forward, ...backward];

          expect(
            stops.filter((stop) => !stop.inside),
            'Tab / Shift+Tab stops outside the dialog',
          ).toEqual([]);
          const visited = new Set(forward.map((stop) => stop.control));
          const unreached = controls
            .filter((control) => !visited.has(String(control.index)))
            .map((control) => control.description);
          expect(unreached, 'dialog controls Tab never reached').toEqual([]);
          const visitedBack = new Set(backward.map((stop) => stop.control));
          const unreachedBack = controls
            .filter((control) => !visitedBack.has(String(control.index)))
            .map((control) => control.description);
          expect(unreachedBack, 'dialog controls Shift+Tab never reached').toEqual([]);

          const badOutlines = stops.flatMap((stop) => {
            const control = controls.find((item) => String(item.index) === stop.control);
            const problem = outlineProblem(stop, focusColor);
            return problem ? [`${control?.description ?? stop.control}: ${problem}`] : [];
          });
          expect(badOutlines, 'controls without a visible 2px ink.focus outline').toEqual([]);

          await cell.keyboard.press('Escape');
          await expect(cell.getByRole('dialog')).toHaveCount(0);
          expect(
            await cell.evaluate(
              () => document.activeElement?.hasAttribute('data-tn-opener') === true,
            ),
            'focus is back on the opener after Esc',
          ).toBe(true);
        } finally {
          await cell.close();
        }
      });
    }
  }

  test('1440px en-US: the toast close control wears the ink.focus ring', async ({ page }) => {
    test.setTimeout(90_000);
    const cell = await openRoute(page, { path: SETTINGS, mockOverrides: MOCKS });
    try {
      await raiseToast(cell);
      const focusColor = await cell.evaluate(resolveInkFocus);
      expect(focusColor, 'the --ink-focus token resolves').not.toBeNull();
      const close = await focusToastClose(cell);
      expect(close, 'Tab from the toast viewport (F8) reaches the close control').not.toBeNull();
      // focus:opacity-100 fades in over the control's own opacity transition.
      await expect
        .poll(
          () => cell.evaluate(() => getComputedStyle(document.activeElement).opacity),
          { message: 'the focused close control is shown' },
        )
        .toBe('1');
      expect(outlineProblem(close, focusColor), 'toast close control outline').toBe('');
    } finally {
      await cell.close();
    }
  });

  for (const width of WIDTHS) {
    for (const locale of LOCALES) {
      test(`${width}px ${locale}: long footer labels wrap without overflow`, async ({ page }) => {
        test.setTimeout(90_000);
        const cell = await openRoute(page, {
          path: SETTINGS,
          width,
          locale,
          mockOverrides: MOCKS,
        });
        try {
          await assertRenderedLocale(cell, locale);
          await openConfirmDialog(cell);
          const measured = await cell.evaluate(applyLongFooterLabel, {
            label: LONG_FOOTER_LABEL[locale],
            cancelLabel: t(locale, 'common.cancel'),
            confirmLabel: t(locale, 'settings.resetPreferences'),
          });

          for (const [index, button] of measured.buttons.entries()) {
            const where = `footer button ${index}`;
            expect(button.lines.length, `${where} stays on one line`).toBeGreaterThan(1);
            for (const line of button.lines) {
              expect(line.left, `${where} line starts outside the content box`).toBeGreaterThanOrEqual(
                button.content.left - 0.5,
              );
              expect(line.right, `${where} line ends outside the content box`).toBeLessThanOrEqual(
                button.content.right + 0.5,
              );
            }
            expect(
              button.content.bottom,
              `${where} last line is vertically clipped`,
            ).toBeGreaterThanOrEqual(button.lines.at(-1).bottom - 0.5);
            expect(button.scrollWidth, `${where} overflows horizontally`).toBeLessThanOrEqual(
              button.clientWidth + 1,
            );
          }
          expect(measured.footer.scrollWidth, 'footer overflows horizontally').toBeLessThanOrEqual(
            measured.footer.clientWidth + 1,
          );
          expect(measured.dialog.left).toBeGreaterThanOrEqual(-0.5);
          expect(measured.dialog.right).toBeLessThanOrEqual(measured.viewportWidth + 0.5);
        } finally {
          await cell.close();
        }
      });
    }
  }

  // Locale does not change motion, so one locale per width.
  for (const width of WIDTHS) {
    test(`${width}px: reduced motion leaves no computed duration > 0 on dialog or toast`, async ({
      page,
    }) => {
      test.setTimeout(90_000);
      const cell = await openRoute(page, {
        path: SETTINGS,
        width,
        reducedMotion: 'reduce',
        mockOverrides: MOCKS,
      });
      try {
        await openConfirmDialog(cell);
        expect(
          await cell.evaluate(motionOffenders),
          'nonzero durations with the dialog open',
        ).toEqual([]);

        await cell.keyboard.press('Escape');
        await expect(cell.getByRole('dialog')).toHaveCount(0);
        await raiseToast(cell);
        expect(
          await cell.evaluate(motionOffenders),
          'nonzero durations with the toast visible',
        ).toEqual([]);
      } finally {
        await cell.close();
      }
    });
  }

  test('S13 geometry rules reject pre-S13 surfaces and accept the contract', () => {
    const contract = {
      elevated: 'rgb(40, 44, 52)',
      shadowLayer: {
        color: 'rgba(15, 17, 21, 0.45)',
        offsetX: '0px',
        offsetY: '8px',
        blur: '24px',
        spread: '0px',
      },
      radiusCap: 6,
      inks: ['rgb(58, 65, 80)'],
    };
    // What the primitives wear before S13: the page fill, Tailwind's shadow-lg,
    // a Tailwind red edge, and the 24px sheet corners.
    const preS13 = {
      backgroundColor: 'rgb(15, 17, 21)',
      boxShadow: 'rgba(0, 0, 0, 0.1) 0px 10px 15px -3px, rgba(0, 0, 0, 0.1) 0px 4px 6px -4px',
      borderColor: 'rgb(220, 38, 38)',
      borderWidth: '1px',
      radii: ['24px', '24px', '0px', '0px'],
    };
    const rejected = geometryProblems(preS13, contract);
    expect(rejected.some((problem) => problem.startsWith('fill '))).toBe(true);
    expect(rejected.some((problem) => problem.startsWith('shadow '))).toBe(true);
    expect(rejected.some((problem) => problem.startsWith('border '))).toBe(true);
    expect(rejected.some((problem) => problem.startsWith('radius '))).toBe(true);

    const accepted = geometryProblems(
      {
        backgroundColor: contract.elevated,
        boxShadow: 'rgba(15, 17, 21, 0.45) 0px 8px 24px 0px',
        borderColor: contract.inks[0],
        borderWidth: '1px',
        radii: ['2px', '2px', '2px', '2px'],
      },
      contract,
    );
    expect(accepted).toEqual([]);
  });

  // Locale does not change geometry, so one locale per theme and width.
  for (const theme of THEMES) {
    for (const width of WIDTHS) {
      test(`${width}px ${theme}: dialog and toast use the S13 floating geometry`, async ({
        page,
      }) => {
        test.setTimeout(90_000);
        const cell = await openRoute(page, {
          path: SETTINGS,
          theme,
          width,
          mockOverrides: MOCKS,
        });
        try {
          await assertRenderedTheme(cell, theme);
          const { base, ruleInks, lossInks } = await floatingContract(cell);
          await openConfirmDialog(cell);
          const dialog = await cell.evaluate(surfaceGeometry, '[role="dialog"]');
          await cell.keyboard.press('Escape');
          await expect(cell.getByRole('dialog')).toHaveCount(0);
          await raiseToast(cell);
          const toast = await cell.evaluate(surfaceGeometry, '.toast-slide');

          expect(
            geometryProblems(dialog, { ...base, inks: ruleInks }),
            `dialog S13 geometry (${dialog.className})`,
          ).toEqual([]);
          expect(
            geometryProblems(toast, { ...base, inks: [...ruleInks, ...lossInks] }),
            `toast S13 geometry (${toast.className})`,
          ).toEqual([]);
        } finally {
          await cell.close();
        }
      });
    }
  }
});
