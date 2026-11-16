/**
 * S25 acceptance: /settings in the terminal-native system.
 *
 *  - ConnectedAccounts and its neighbour panels use the token type size
 *    (--size-chrome) instead of the ad-hoc text-[0.625rem] / [0.6875rem] /
 *    [0.8125rem] sizes, and token radii instead of rounded / -md / -lg / -xl;
 *  - Settings.css keeps both console tracks at minmax(0, ...) and the nav at
 *    min-width 0, so 390px does not scroll sideways without S01's override;
 *  - the shell copy comes from the tnSettings pair, in en-US and zh-CN;
 *  - the section index is a row of real buttons that Tab reaches and that
 *    still switch the panel (behaviour unchanged).
 */
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { Route, Routes } from 'react-router';
import i18n from 'i18next';
import { renderWithProviders } from '@/test/utils';

vi.mock('@/hooks/useUser', () => ({ useUser: () => ({ isLoading: false }) }));
vi.mock('@/hooks/usePreferences', () => ({ usePreferences: () => ({ isLoading: false }) }));
vi.mock('../panels/UserInfoTab', () => ({ UserInfoTab: () => <div data-testid="panel-userInfo" /> }));
vi.mock('../panels/PreferencesTab', () => ({ PreferencesTab: () => <div data-testid="panel-preferences" /> }));
vi.mock('../panels/ModelTab', () => ({ ModelTab: () => <div data-testid="panel-model" /> }));
vi.mock('../panels/ExperimentsTab', () => ({ ExperimentsTab: () => <div data-testid="panel-experiments" /> }));
vi.mock('../panels/NativeRuntimeInventory', () => ({
  NativeRuntimeInventory: () => <div data-testid="panel-native" />,
}));
vi.mock('../panels/AboutTab', () => ({ AboutTab: () => <div data-testid="panel-about" /> }));
vi.mock('../panels/RoleRegistryTab', () => ({ RoleRegistryTab: () => <div data-testid="panel-roles" /> }));

import Settings from '../Settings';

const HERE = dirname(fileURLToPath(import.meta.url));
const SETTINGS_DIR = resolve(HERE, '..');
const PANELS_DIR = join(SETTINGS_DIR, 'panels');
const read = (p: string) => readFileSync(p, 'utf8');

const panelSources = readdirSync(PANELS_DIR)
  .filter((f) => f.endsWith('.tsx'))
  .map((f) => ({ file: f, src: read(join(PANELS_DIR, f)) }));

/** The body of every `@media (max-width: 900px)` block in a stylesheet. */
function narrowBlocks(css: string): string {
  const out: string[] = [];
  const re = /@media \(max-width: 900px\)\s*\{/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(css))) {
    let depth = 1;
    let i = m.index + m[0].length;
    const start = i;
    while (depth > 0 && i < css.length) {
      if (css[i] === '{') depth += 1;
      if (css[i] === '}') depth -= 1;
      i += 1;
    }
    out.push(css.slice(start, i - 1));
  }
  return out.join('\n');
}

/** Declarations of one top-level rule, e.g. `.settings-console-nav`. */
function ruleBody(css: string, selector: string): string {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const m = new RegExp(`(^|\\n)${esc}\\s*\\{([^}]*)\\}`).exec(css);
  return m ? m[2] : '';
}

describe('S25 token type sizes and geometry', () => {
  it('ConnectedAccounts.tsx has no text-[0.6…] ad-hoc size (AUDIT §6 #9)', () => {
    const src = read(join(PANELS_DIR, 'ConnectedAccounts.tsx'));
    expect(src.match(/text-\[0\.6/g) ?? []).toEqual([]);
    expect(src).toMatch(/text-\(length:--size-chrome\)/);
  });

  it('no Settings panel uses an arbitrary rem text size', () => {
    const offenders = panelSources
      .flatMap(({ file, src }) => (src.match(/text-\[[\d.]+rem\]/g) ?? []).map((m) => `${file}: ${m}`));
    expect(offenders).toEqual([]);
  });

  it('no Settings panel uses Tailwind rounding above the token radii', () => {
    const offenders = panelSources.flatMap(({ file, src }) =>
      (src.match(/\brounded(?:-(?:sm|md|lg|xl|2xl|3xl)\b|(?![-\w(]))/g) ?? []).map((m) => `${file}: ${m}`),
    );
    expect(offenders).toEqual([]);
  });

  it('no Settings panel carries a literal hex colour (not even as a var() fallback)', () => {
    const offenders = panelSources.flatMap(({ file, src }) =>
      (src.match(/#[0-9a-fA-F]{3,8}\b(?=['"),;\s])/g) ?? []).map((m) => `${file}: ${m}`),
    );
    expect(offenders).toEqual([]);
  });

  it('no Settings panel renders hardcoded CJK JSX text (en-US must not show zh copy)', () => {
    const offenders = panelSources.flatMap(({ file, src }) =>
      (src.match(/>[^<>{}]*[一-鿿][^<>{}]*</g) ?? []).map((m) => `${file}: ${m}`),
    );
    expect(offenders).toEqual([]);
  });

  it('keeps a 999px pill only on the native runtime sign-in state tag', () => {
    const pills = panelSources.flatMap(({ file, src }) =>
      (src.match(/rounded-full/g) ?? []).map(() => file),
    );
    expect(pills).toEqual(['NativeRuntimeInventory.tsx']);
  });

  it('Settings.css and ConnectedAccounts.css use token sizes, radii and no literal colours', () => {
    for (const name of ['Settings.css', 'panels/ConnectedAccounts.css']) {
      const css = read(join(SETTINGS_DIR, name));
      expect(css, `${name}: literal hex`).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
      expect(css, `${name}: rem font size`).not.toMatch(/font-size:\s*[\d.]+rem/);
      expect(css, `${name}: literal radius`).not.toMatch(/border-radius:\s*\d+px/);
    }
  });

  it('Settings.css transitions read the motion tokens (zeroed under reduced motion)', () => {
    const css = read(join(SETTINGS_DIR, 'Settings.css'));
    const transitions = css.match(/transition:[^;]+;/g) ?? [];
    expect(transitions.length).toBeGreaterThan(0);
    for (const t of transitions) {
      expect(t).not.toMatch(/\d+m?s\b/);
      expect(t).toMatch(/var\(--duration-quick\)/);
    }
  });
});

describe('S25 narrow track lives in Settings.css', () => {
  const css = read(join(SETTINGS_DIR, 'Settings.css'));

  it('collapses the console to minmax(0, 1fr), never a bare 1fr', () => {
    const narrow = narrowBlocks(css);
    expect(narrow).toMatch(/\.settings-console\s*\{\s*grid-template-columns: minmax\(0, 1fr\);/);
    expect(css).not.toMatch(/grid-template-columns:\s*1fr\s*;/);
    expect(ruleBody(css, '.settings-console')).toMatch(/grid-template-columns: 240px minmax\(0, 1fr\);/);
  });

  it('pins min-width: 0 on the nav and its tab strip so the strip scrolls in its own box', () => {
    expect(ruleBody(css, '.settings-console-nav')).toMatch(/min-width: 0;/);
    expect(ruleBody(css, '.settings-tab-list')).toMatch(/min-width: 0;/);
    expect(narrowBlocks(css)).toMatch(/\.settings-tab-list\s*\{[^}]*overflow-x: auto;/);
  });

  it('lets a row shrink-0 control column and segmented group shrink to the row (390 fit)', () => {
    expect(css).toMatch(
      /\.settings-row > \.shrink-0,\s*\.settings-row \[role="group"\]\.clips-focus-ring\s*\{\s*flex-shrink: 1;\s*max-width: 100%;/,
    );
    expect(css).toMatch(/\.settings-row \[role="group"\]\.clips-focus-ring > button\s*\{[^}]*white-space: normal;/);
  });
});

describe('S25 Settings shell', () => {
  afterEach(async () => {
    await i18n.changeLanguage('en-US');
  });

  function renderSettings(route = '/settings') {
    return renderWithProviders(
      <Routes>
        <Route path="/settings" element={<Settings />} />
      </Routes>,
      { route },
    );
  }

  it('renders the tnSettings eyebrows and nav label in en-US', async () => {
    await i18n.changeLanguage('en-US');
    renderSettings('/settings?tab=model');
    expect(screen.getByText('conductor / settings')).toBeInTheDocument();
    expect(screen.getByText('settings / model')).toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: 'Settings sections' })).toBeInTheDocument();
    expect(screen.queryByText('CONDUCTOR / SETTINGS')).toBeNull();
  });

  it('renders the tnSettings eyebrows and nav label in zh-CN', async () => {
    await i18n.changeLanguage('zh-CN');
    renderSettings('/settings?tab=about');
    expect(screen.getByText('conductor / 设置')).toBeInTheDocument();
    expect(screen.getByText('设置 / about')).toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: '设置分区' })).toBeInTheDocument();
  });

  it('the section index is keyboard-reachable buttons that still switch the panel', async () => {
    await i18n.changeLanguage('en-US');
    renderSettings('/settings');
    const nav = screen.getByRole('navigation', { name: 'Settings sections' });
    const tabs = Array.from(nav.querySelectorAll('button'));
    expect(tabs).toHaveLength(7);
    for (const tab of tabs) {
      expect(tab).toHaveAttribute('type', 'button');
      expect(tab.tabIndex).toBe(0);
    }
    expect(screen.getByTestId('panel-userInfo')).toBeInTheDocument();
    expect(tabs[0]).toHaveAttribute('aria-current', 'page');

    tabs[2].focus();
    expect(tabs[2]).toHaveFocus();
    fireEvent.click(tabs[2]);
    expect(screen.getByTestId('panel-model')).toBeInTheDocument();
    expect(screen.queryByTestId('panel-userInfo')).toBeNull();
    expect(tabs[2]).toHaveAttribute('aria-current', 'page');
    expect(tabs[0]).not.toHaveAttribute('aria-current');
  });
});
