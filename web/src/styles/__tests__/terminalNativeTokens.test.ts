// @vitest-environment node
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/**
 * terminal-native.css must be a verbatim projection of tokens.json: every
 * colour property equals its theme value, and the only #hex literals allowed
 * in the three owned stylesheets are the 25 token colours.
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB_SRC = resolve(HERE, '../..');
const REPO_ROOT = resolve(WEB_SRC, '../..');
const TOKENS_JSON = join(REPO_ROOT, 'docs/design/terminal-native/system/tokens.json');
const TERMINAL_NATIVE_CSS = join(WEB_SRC, 'styles/terminal-native.css');
const TOKENS_CSS = join(WEB_SRC, 'styles/tokens.css');
const INDEX_CSS = join(WEB_SRC, 'index.css');

type ThemeName = 'dark' | 'light';

interface TokensFile {
  themes: Record<
    ThemeName,
    {
      surfaces: Record<string, string>;
      ink: Record<string, string>;
    }
  >;
  space: Array<{ token: string; px: number }>;
  radius: Record<string, { px: number }>;
  border: {
    hairline: { px: number };
    focus: { px: number; offsetPx: number };
  };
  elevation: {
    '0': { shadow: null };
    '1': { dark: string; light: string };
  };
  motion: {
    ease: Record<string, number[]>;
    durationMs: Record<string, number>;
  };
  typography: {
    mono: {
      family: string;
      fallbacks: string[];
      sizePx: number;
      lineHeight: number;
      weightLabel: number;
      weightValue: number;
    };
    prose: {
      family: string;
      fallbacks: string[];
      sizePx: number;
      lineHeight: number;
    };
  };
}

function kebab(key: string): string {
  return key.replace(/[A-Z]/g, (ch) => `-${ch.toLowerCase()}`);
}

function parseBlock(css: string, selector: string): Map<string, string> {
  const start = css.indexOf(selector);
  if (start < 0) throw new Error(`missing selector ${selector}`);
  const brace = css.indexOf('{', start);
  let depth = 0;
  let end = -1;
  for (let i = brace; i < css.length; i++) {
    if (css[i] === '{') depth++;
    else if (css[i] === '}') {
      depth--;
      if (depth === 0) {
        end = i;
        break;
      }
    }
  }
  if (end < 0) throw new Error(`unclosed block for ${selector}`);
  const body = css.slice(brace + 1, end);
  const props = new Map<string, string>();
  for (const match of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    props.set(match[1], match[2].trim());
  }
  return props;
}

function cubicBezier(parts: number[]): string {
  return `cubic-bezier(${parts.join(', ')})`;
}

function fontStack(family: string, fallbacks: string[]): string {
  const quote = (name: string) => (/\s/.test(name) ? `"${name}"` : name);
  return [family, ...fallbacks].map(quote).join(', ');
}

function normalizeCssValue(value: string): string {
  return value.replace(/\s+/g, ' ').trim().toLowerCase();
}

describe('terminal-native tokens', () => {
  const tokens = JSON.parse(readFileSync(TOKENS_JSON, 'utf8')) as TokensFile;
  const css = readFileSync(TERMINAL_NATIVE_CSS, 'utf8');
  const root = parseBlock(css, ':root');
  const light = parseBlock(css, '[data-theme="light"]');
  const reduced = parseBlock(css, '@media (prefers-reduced-motion: reduce)');

  const allowedHex = new Set<string>();
  for (const theme of Object.values(tokens.themes)) {
    for (const hex of Object.values(theme.surfaces)) allowedHex.add(hex.toUpperCase());
    for (const hex of Object.values(theme.ink)) allowedHex.add(hex.toUpperCase());
  }

  it('has exactly the 25 token hex values', () => {
    expect(allowedHex.size).toBe(25);
  });

  it('declares every dark colour property as its token value on :root', () => {
    const dark = tokens.themes.dark;
    for (const [name, hex] of Object.entries(dark.surfaces)) {
      expect(root.get(`--surface-${kebab(name)}`)).toBe(hex);
    }
    for (const [name, hex] of Object.entries(dark.ink)) {
      expect(root.get(`--ink-${kebab(name)}`)).toBe(hex);
    }
  });

  it('declares every light colour property as its token value', () => {
    const theme = tokens.themes.light;
    for (const [name, hex] of Object.entries(theme.surfaces)) {
      expect(light.get(`--surface-${kebab(name)}`)).toBe(hex);
    }
    for (const [name, hex] of Object.entries(theme.ink)) {
      expect(light.get(`--ink-${kebab(name)}`)).toBe(hex);
    }
  });

  it('declares shared scales from tokens.json on :root', () => {
    const mono = tokens.typography.mono;
    const prose = tokens.typography.prose;
    expect(normalizeCssValue(root.get('--font-mono')!)).toBe(
      normalizeCssValue(fontStack(mono.family, mono.fallbacks)),
    );
    expect(normalizeCssValue(root.get('--font-prose')!)).toBe(
      normalizeCssValue(fontStack(prose.family, prose.fallbacks)),
    );
    expect(root.get('--size-chrome')).toBe(`${mono.sizePx}px`);
    expect(root.get('--lh-chrome')).toBe(String(mono.lineHeight));
    expect(root.get('--size-prose')).toBe(`${prose.sizePx}px`);
    expect(root.get('--lh-prose')).toBe(String(prose.lineHeight));
    expect(root.get('--weight-label')).toBe(String(mono.weightLabel));
    expect(root.get('--weight-value')).toBe(String(mono.weightValue));

    for (const entry of tokens.space) {
      const name = `--${entry.token.replaceAll('.', '-')}`;
      expect(root.get(name)).toBe(`${entry.px}px`);
    }
    for (const [name, value] of Object.entries(tokens.radius)) {
      expect(root.get(`--radius-${kebab(name)}`)).toBe(`${value.px}px`);
    }
    expect(root.get('--border-hairline')).toBe(`${tokens.border.hairline.px}px`);
    expect(root.get('--border-focus')).toBe(`${tokens.border.focus.px}px`);
    expect(root.get('--focus-offset')).toBe(`${tokens.border.focus.offsetPx}px`);
    expect(root.get('--elevation-0')).toBe('none');
    expect(normalizeCssValue(root.get('--elevation-1')!)).toBe(
      normalizeCssValue(tokens.elevation['1'].dark),
    );
    expect(normalizeCssValue(light.get('--elevation-1')!)).toBe(
      normalizeCssValue(tokens.elevation['1'].light),
    );

    for (const [name, parts] of Object.entries(tokens.motion.ease)) {
      expect(normalizeCssValue(root.get(`--ease-${kebab(name)}`)!)).toBe(
        normalizeCssValue(cubicBezier(parts)),
      );
    }
    for (const [name, ms] of Object.entries(tokens.motion.durationMs)) {
      expect(root.get(`--duration-${kebab(name)}`)).toBe(`${ms}ms`);
    }
  });

  it('zeroes motion durations under prefers-reduced-motion', () => {
    for (const name of Object.keys(tokens.motion.durationMs)) {
      expect(reduced.get(`--duration-${kebab(name)}`)).toBe('0ms');
    }
  });

  it('allows only the 25 token hex values in owned stylesheets', () => {
    const files = [
      ['terminal-native.css', readFileSync(TERMINAL_NATIVE_CSS, 'utf8')],
      ['tokens.css', readFileSync(TOKENS_CSS, 'utf8')],
      ['index.css', readFileSync(INDEX_CSS, 'utf8')],
    ] as const;
    const foreign: string[] = [];
    for (const [label, source] of files) {
      for (const match of source.matchAll(/#([0-9A-Fa-f]{3,8})\b/g)) {
        const hex = `#${match[1].toUpperCase()}`;
        // Expand 3-digit hex for comparison against the 6-digit token set.
        const full =
          hex.length === 4
            ? `#${hex[1]}${hex[1]}${hex[2]}${hex[2]}${hex[3]}${hex[3]}`
            : hex;
        if (!allowedHex.has(full)) {
          foreign.push(`${label}: ${match[0]}`);
        }
      }
    }
    expect(foreign).toEqual([]);
  });

  it('aliases market up/down solids to ink tokens and soft fills to color-mix', () => {
    const tokensCss = readFileSync(TOKENS_CSS, 'utf8');
    const root = parseBlock(tokensCss, ':root');
    expect(root.get('--color-up')).toBe('var(--ink-ok)');
    expect(root.get('--color-down')).toBe('var(--ink-error)');
    expect(root.get('--color-up-soft')).toMatch(/^color-mix\(in srgb, var\(--ink-ok\)/);
    expect(root.get('--color-down-soft')).toMatch(/^color-mix\(in srgb, var\(--ink-error\)/);
  });

  it('keeps soft accent/semantic fills as color-mix (not equal to their fg ink)', () => {
    const root = parseBlock(readFileSync(TOKENS_CSS, 'utf8'), ':root');
    const pairs: Array<[string, string, string]> = [
      ['--color-accent-soft', '--color-accent-primary', '--ink-baton'],
      ['--color-success-soft', '--color-success', '--ink-ok'],
      ['--color-danger-soft', '--color-btn-danger', '--ink-error'],
      ['--color-warning-soft', '--color-warning', '--ink-hold'],
      ['--color-info-soft', '--color-info', '--ink-tempo'],
      ['--color-profit-soft', '--color-profit', '--ink-ok'],
      ['--color-loss-soft', '--color-loss', '--ink-error'],
    ];
    for (const [soft, solid, ink] of pairs) {
      expect(root.get(solid), solid).toBe(`var(${ink})`);
      expect(root.get(soft), soft).toMatch(new RegExp(`^color-mix\\(in srgb, var\\(${ink}\\)`));
      expect(root.get(soft), `${soft} ≠ ${solid}`).not.toBe(root.get(solid));
    }
  });

  it('maps quaternary text to readable ink.muted, not decorative ink.faint', () => {
    const root = parseBlock(readFileSync(TOKENS_CSS, 'utf8'), ':root');
    expect(root.get('--color-text-quaternary')).toBe('var(--ink-muted)');
    expect(root.get('--color-text-quaternary')).not.toBe('var(--ink-faint)');
  });

  it('derives the shadcn bridge as full token colours (Chrome 111-safe)', () => {
    const root = parseBlock(readFileSync(TOKENS_CSS, 'utf8'), ':root');
    const tokensCss = readFileSync(TOKENS_CSS, 'utf8');
    const indexCss = readFileSync(INDEX_CSS, 'utf8');
    // Relative colour syntax needs Chrome 119+; keep it out of owned stylesheets.
    expect(tokensCss).not.toMatch(/\bfrom\s+var\(/);
    expect(indexCss).not.toMatch(/\bfrom\s+var\(/);
    expect(indexCss).not.toMatch(/hsl\(var\(--/);

    const bridge: Record<string, string> = {
      '--background': 'var(--surface-page)',
      '--foreground': 'var(--ink-fg)',
      '--primary': 'var(--ink-baton)',
      '--primary-foreground': 'var(--ink-on-accent)',
      '--destructive': 'var(--ink-error)',
      '--ring': 'var(--ink-focus)',
      '--card': 'var(--surface-elevated)',
      '--popover': 'var(--surface-elevated)',
      '--input': 'var(--surface-canvas)',
      '--shadow-elevated': 'var(--elevation-1)',
      '--shadow-card': 'var(--elevation-1)',
    };
    for (const [name, value] of Object.entries(bridge)) {
      expect(root.get(name), name).toBe(value);
    }
    const light = parseBlock(tokensCss, '[data-theme="light"]');
    for (const name of Object.keys(bridge)) {
      expect(light.has(name), `light overrides ${name}`).toBe(false);
    }
  });

  it('keeps --bg-main as the HSL channels of surface.page in both themes (App.css hsl(var(--bg-main)))', () => {
    const tokensCss = readFileSync(TOKENS_CSS, 'utf8');
    const blocks = {
      dark: parseBlock(tokensCss, ':root'),
      light: parseBlock(tokensCss, '[data-theme="light"]'),
    };
    for (const theme of ['dark', 'light'] as const) {
      const declared = blocks[theme].get('--bg-main');
      expect(declared, `${theme} --bg-main`).toMatch(/^[\d.]+ [\d.]+% [\d.]+%$/);
      const [h, s, l] = declared!.split(' ').map((part) => parseFloat(part));
      const rgb = hslToRgb(h, s / 100, l / 100);
      const want = parseHex(tokens.themes[theme].surfaces.page);
      for (let i = 0; i < 3; i++) {
        expect(Math.abs(rgb[i] - want[i]), `${theme} --bg-main channel ${i}`).toBeLessThan(0.5);
      }
    }
  });

  it('draws the focus-defaults ring from the border.focus tokens, not literals', () => {
    const tokensCss = readFileSync(TOKENS_CSS, 'utf8');
    const layer = tokensCss.slice(tokensCss.indexOf('@layer focus-defaults'));
    expect(layer).toMatch(/:focus-visible\s*\{\s*outline: var\(--border-focus\) solid var\(--color-focus-ring\);\s*outline-offset: var\(--focus-offset\);/);
    expect(layer).not.toMatch(/outline: 2px solid var\(--color-focus-ring\)/);
    expect(layer).not.toMatch(/outline-offset: 2px;/);
  });

  // S25 moved the Settings 390px fix into Settings.css (minmax(0, 1fr) track,
  // min-width: 0 on the nav) and deleted S01's temporary "Narrow settings"
  // override from tokens.css. It must not come back.
  it('no longer carries the temporary Settings 390px override (S25 removed it)', () => {
    const tokensCss = readFileSync(TOKENS_CSS, 'utf8');
    expect(tokensCss).not.toMatch(/Narrow settings/);
    expect(tokensCss).not.toMatch(/:root \.settings-console\s*\{/);
    expect(tokensCss).not.toMatch(/:root \.settings-console-nav,/);
    expect(tokensCss).not.toMatch(/\.settings-/);
  });

  it('aliases fonts and Labels to terminal-native typography / ink', () => {
    const root = parseBlock(readFileSync(TOKENS_CSS, 'utf8'), ':root');
    expect(root.get('--font-ui')).toBe('var(--font-mono)');
    expect(root.get('--font-content')).toBe('var(--font-prose)');
    // Titles are chrome (SPEC.md Type): mono. Settings.css and Plugins.css read it.
    expect(root.get('--font-display')).toBe('var(--font-mono)');
    // Labels are text: solid ink.muted, not a translucent mix of ink.fg.
    expect(root.get('--Labels-Secondary')).toBe('var(--ink-muted)');
    expect(root.get('--Labels-Tertiary')).toBe('var(--ink-muted)');
    expect(root.get('--Labels-Quaternary')).toBe('var(--ink-muted)');
  });

  it('keeps dark overlay-strong at least as opaque as overlay (~0.6 / ~0.8)', () => {
    const root = parseBlock(readFileSync(TOKENS_CSS, 'utf8'), ':root');
    const light = parseBlock(readFileSync(TOKENS_CSS, 'utf8'), '[data-theme="light"]');
    const pct = (value: string | undefined) => {
      const m = value?.match(/color-mix\(in srgb,\s*var\(--[\w-]+\)\s+(\d+)%/);
      expect(m, `parse ${value}`).toBeTruthy();
      return Number(m![1]);
    };
    expect(root.get('--color-bg-overlay')).toMatch(/var\(--ink-on-accent\)/);
    expect(root.get('--color-bg-overlay-strong')).toMatch(/var\(--ink-on-accent\)/);
    expect(pct(root.get('--color-bg-overlay'))).toBe(60);
    expect(pct(root.get('--color-bg-overlay-strong'))).toBe(80);
    expect(pct(root.get('--color-bg-overlay-strong'))).toBeGreaterThanOrEqual(
      pct(root.get('--color-bg-overlay')),
    );
    expect(pct(light.get('--color-bg-overlay-strong'))).toBeGreaterThanOrEqual(
      pct(light.get('--color-bg-overlay')),
    );
  });

  it('pins sheet paper/ink via invariant --sheet-* aliases', () => {
    const tn = parseBlock(readFileSync(TERMINAL_NATIVE_CSS, 'utf8'), ':root');
    const root = parseBlock(readFileSync(TOKENS_CSS, 'utf8'), ':root');
    const lightTn = parseBlock(readFileSync(TERMINAL_NATIVE_CSS, 'utf8'), '[data-theme="light"]');
    const light = parseBlock(readFileSync(TOKENS_CSS, 'utf8'), '[data-theme="light"]');
    expect(tn.get('--sheet-paper')).toBe(tokens.themes.light.surfaces.page);
    expect(tn.get('--sheet-ink')).toBe(tokens.themes.light.ink.fg);
    expect(lightTn.has('--sheet-paper')).toBe(false);
    expect(lightTn.has('--sheet-ink')).toBe(false);
    expect(root.get('--color-sheet-paper')).toBe('var(--sheet-paper)');
    expect(root.get('--color-sheet-ink')).toBe('var(--sheet-ink)');
    expect(light.has('--color-sheet-paper')).toBe(false);
    expect(light.has('--color-sheet-ink')).toBe(false);
  });

  it('zeroes hard-coded transitions under prefers-reduced-motion', () => {
    const css = readFileSync(TERMINAL_NATIVE_CSS, 'utf8');
    const reduced = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'));
    expect(reduced).toMatch(/transition-duration:\s*0s\s*!important/);
    expect(reduced).toMatch(/animation-duration:\s*0s\s*!important/);
    for (const name of Object.keys(tokens.motion.durationMs)) {
      expect(reduced).toMatch(new RegExp(`--duration-${kebab(name)}:\\s*0ms`));
    }
  });

  it('exposes space tokens under --spacing-space-* without colliding with p-5 / w-6', async () => {
    const indexCss = readFileSync(INDEX_CSS, 'utf8');
    expect(indexCss).not.toMatch(/--spacing-[0-6]\s*:/);
    for (const entry of tokens.space) {
      const n = entry.token.split('.')[1];
      expect(indexCss).toMatch(
        new RegExp(`--spacing-space-${n}\\s*:\\s*var\\(--space-${n}\\)`),
      );
    }
    const out = await compileUtilities(['p-5', 'w-6', 'p-space-5', 'w-space-6'], {
      themeInline: `
    --spacing-space-5: var(--space-5);
    --spacing-space-6: var(--space-6);
`,
      importDefaultTheme: true,
    });
    expect(out).toMatch(/\.p-5\s*\{[^}]*padding:\s*calc\(var\(--spacing\)\s*\*\s*5\)/);
    expect(out).toMatch(/\.w-6\s*\{[^}]*width:\s*calc\(var\(--spacing\)\s*\*\s*6\)/);
    expect(out).toContain('.p-space-5');
    expect(out).toContain('.w-space-6');
    expect(out).toContain('var(--space-5)');
    expect(out).toContain('var(--space-6)');
    expect(out).not.toMatch(/\.p-5\s*\{[^}]*24px/);
    expect(out).not.toMatch(/\.w-6\s*\{[^}]*32px/);
  });

  it('exposes motion durations as --transition-duration-* for Tailwind utilities', async () => {
    const indexCss = readFileSync(INDEX_CSS, 'utf8');
    for (const name of Object.keys(tokens.motion.durationMs)) {
      expect(indexCss).toMatch(
        new RegExp(`--transition-duration-${kebab(name)}\\s*:\\s*var\\(--duration-${kebab(name)}\\)`),
      );
    }
    const themeInline = Object.keys(tokens.motion.durationMs)
      .map((n) => `    --transition-duration-${kebab(n)}: var(--duration-${kebab(n)});`)
      .join('\n');
    const classes = Object.keys(tokens.motion.durationMs).map((n) => `duration-${kebab(n)}`);
    const out = await compileUtilities(classes, { themeInline });
    for (const name of Object.keys(tokens.motion.durationMs)) {
      expect(out).toContain(`.duration-${kebab(name)}`);
      expect(out).toContain(`var(--duration-${kebab(name)})`);
    }
  });

  it('compiles rounded-control, font-mono and ease-standard without theme-layer cycles', async () => {
    const indexCss = readFileSync(INDEX_CSS, 'utf8');
    expect(indexCss).not.toMatch(/--radius-control\s*:\s*var\(--radius-control\)/);
    expect(indexCss).not.toMatch(/--font-mono\s*:\s*var\(--font-mono\)/);
    expect(indexCss).not.toMatch(/--ease-standard\s*:\s*var\(--ease-standard\)/);
    expect(indexCss).toMatch(/@utility\s+rounded-control/);
    expect(indexCss).toMatch(/@utility\s+ease-standard/);

    const out = await compileUtilities(['rounded-control', 'font-mono', 'ease-standard'], {
      extras: `
@utility rounded-control { border-radius: var(--radius-control); }
@utility ease-standard { transition-timing-function: var(--ease-standard); }
`,
      importDefaultTheme: true,
    });
    expect(out).toMatch(/\.rounded-control\s*\{\s*border-radius:\s*var\(--radius-control\)/);
    expect(out).toMatch(/\.font-mono\s*\{\s*font-family:\s*var\(--font-mono\)/);
    expect(out).toMatch(
      /\.ease-standard\s*\{[^}]*transition-timing-function:\s*var\(--ease-standard\)/,
    );
    // Theme layer must not reintroduce a self-referential custom property.
    expect(out).not.toMatch(
      /@layer theme\s*\{[^}]*--radius-control:\s*var\(--radius-control\)/,
    );
    expect(out).not.toMatch(/@layer theme\s*\{[^}]*--ease-standard:\s*var\(--ease-standard\)/);
  });
});

/**
 * Every text alias on every ground it may sit on, both themes, computed from
 * the declared values (SPEC.md Contrast: body text >= 4.5:1). A translucent
 * ground is painted over surface.page, then the text over that.
 */
describe('legacy alias contrast', () => {
  const tn = readFileSync(TERMINAL_NATIVE_CSS, 'utf8');
  const aliases = readFileSync(TOKENS_CSS, 'utf8');
  const themes = {
    dark: new Map([...parseBlock(tn, ':root'), ...parseBlock(aliases, ':root')]),
    light: new Map([
      ...parseBlock(tn, ':root'),
      ...parseBlock(aliases, ':root'),
      ...parseBlock(tn, '[data-theme="light"]'),
      ...parseBlock(aliases, '[data-theme="light"]'),
    ]),
  };

  /** Neutral text inks: allowed on every neutral ground. */
  const NEUTRAL_TEXT = [
    '--color-text-primary',
    '--color-text-secondary',
    '--color-text-tertiary',
    '--color-text-quaternary',
    '--color-text-muted',
    '--foreground',
    '--card-foreground',
    '--popover-foreground',
    '--secondary-foreground',
    '--muted-foreground',
    '--Labels-Secondary',
    '--Labels-Tertiary',
    '--Labels-Quaternary',
  ];

  /** Status / accent inks set as text (labels, pills, identity tiles). */
  const STATUS_TEXT = [
    '--color-accent-primary',
    '--color-accent-light',
    '--color-slash-icon',
    '--color-profit',
    '--color-loss',
    '--color-warning',
    '--color-info',
    '--color-success',
    '--color-btn-danger',
    '--color-btn-stop-hover-fg',
    '--color-up',
    '--color-down',
    '--accent-fin',
    '--primary',
    '--destructive',
    ...Array.from({ length: 8 }, (_, i) => `--color-identity-${i}-fg`),
  ];

  /** Grounds text is laid on. Selection-tinted grounds are listed apart. */
  const NEUTRAL_GROUNDS = [
    '--color-bg-page',
    '--color-bg-canvas',
    '--color-bg-card',
    '--color-bg-tool-card',
    '--color-bg-elevated',
    '--color-bg-input',
    '--color-bg-popover',
    '--color-bg-code',
    '--color-bg-chart-placeholder',
    '--color-bg-surface',
    '--color-bg-hover',
    '--color-bg-subtle',
    '--color-bg-card-gradient',
    '--background',
    '--card',
    '--popover',
    '--secondary',
    '--muted',
    '--input',
    '--bg-panel',
    '--surface-card',
    '--sidebar-bg',
    '--sidebar-hover',
  ];
  const SELECTION_GROUNDS = [
    '--color-bg-card-hover',
    '--color-bg-tag',
    '--surface-card-hover',
    '--color-btn-stop-hover-bg',
  ];

  /** Text on a filled accent / tinted ground of its own. */
  const PAIRED: Array<[string, string]> = [
    ['--primary-foreground', '--primary'],
    ['--accent-foreground', '--accent'],
    ['--destructive-foreground', '--destructive'],
    ['--color-text-on-accent', '--color-accent-primary'],
    ['--color-btn-primary-text', '--color-btn-primary-bg'],
    ['--color-btn-stop-hover-fg', '--color-btn-stop-hover-bg'],
    ['--color-sheet-ink', '--color-sheet-paper'],
    ['--color-accent-primary', '--color-accent-soft'],
    ['--color-success', '--color-success-soft'],
    ['--color-warning', '--color-warning-soft'],
    ['--color-info', '--color-info-soft'],
    ['--color-btn-danger', '--color-danger-soft'],
    ['--color-btn-danger', '--color-danger-hover-bg'],
    ['--color-profit', '--color-profit-soft'],
    ['--color-loss', '--color-loss-soft'],
    ['--color-up', '--color-up-soft'],
    ['--color-down', '--color-down-soft'],
    ['--color-slash-icon', '--color-slash-pill-bg'],
    ...Array.from(
      { length: 8 },
      (_, i) => [`--color-identity-${i}-fg`, `--color-identity-${i}-bg`] as [string, string],
    ),
  ];

  for (const theme of ['dark', 'light'] as const) {
    const props = themes[theme];
    const resolve = makeResolver(props);
    const page = resolve('--surface-page')!;
    const ground = (name: string) => {
      const c = resolve(name);
      if (!c) throw new Error(`${name} is not a colour`);
      return over(c, page);
    };
    const ratio = (text: string, bg: string) => {
      const c = resolve(text);
      if (!c) throw new Error(`${text} is not a colour`);
      return contrast(over(c, ground(bg)), ground(bg));
    };

    const pairs: Array<[string, string]> = [];
    for (const text of [...NEUTRAL_TEXT, ...STATUS_TEXT]) {
      for (const bg of NEUTRAL_GROUNDS) pairs.push([text, bg]);
    }
    // Unconverted pages never repaint a status ink on a selection-tinted
    // ground, so every text alias must clear 4.5:1 there too.
    for (const text of [...NEUTRAL_TEXT, ...STATUS_TEXT]) {
      for (const bg of SELECTION_GROUNDS) pairs.push([text, bg]);
    }
    pairs.push(...PAIRED);

    it(`${theme}: every text alias clears 4.5:1 on its grounds (${pairs.length} pairs)`, () => {
      const failing = pairs
        .map(([text, bg]) => ({ text, bg, ratio: ratio(text, bg) }))
        .filter((p) => p.ratio < 4.5)
        .map((p) => `${p.text} on ${p.bg}: ${p.ratio.toFixed(2)}:1`);
      expect(failing).toEqual([]);
    });

    it(`${theme}: --muted-foreground (placeholders, dialog descriptions) is solid ink.muted`, () => {
      expect(props.get('--muted-foreground')).toBe('var(--ink-muted)');
      expect(resolve('--muted-foreground')![3]).toBe(1);
      expect(ratio('--muted-foreground', '--background')).toBeGreaterThanOrEqual(4.5);
      expect(ratio('--muted-foreground', '--popover')).toBeGreaterThanOrEqual(4.5);
    });

    it(`${theme}: the focus ring clears 3:1 on every ground`, () => {
      const failing = [...NEUTRAL_GROUNDS, ...SELECTION_GROUNDS]
        .map((bg) => ({ bg, ratio: ratio('--color-focus-ring', bg) }))
        .filter((p) => p.ratio < 3)
        .map((p) => `${p.bg}: ${p.ratio.toFixed(2)}:1`);
      expect(failing).toEqual([]);
    });
  }

  it('resolves colour-mix like the browser (sanity)', () => {
    const resolve = makeResolver(
      new Map([
        ['--a', '#FFFFFF'],
        ['--half', 'color-mix(in srgb, var(--a) 50%, transparent)'],
        ['--grey', 'color-mix(in srgb, var(--a) 50%, #000000)'],
      ]),
    );
    expect(resolve('--half')).toEqual([255, 255, 255, 0.5]);
    expect(resolve('--grey')).toEqual([127.5, 127.5, 127.5, 1]);
    // The reviewed defect: 62% ink.muted over the dark page is 3.56:1.
    const dark = makeResolver(
      new Map([
        ['--page', '#0F1115'],
        ['--m', 'color-mix(in srgb, #9AA3B5 62%, transparent)'],
      ]),
    );
    const page = dark('--page')!;
    expect(contrast(over(dark('--m')!, page), page)).toBeLessThan(4.5);
  });
});

function parseHex(hex: string): [number, number, number] {
  const m = hex.match(/^#([0-9a-f]{6})$/i);
  if (!m) throw new Error(`not a 6-digit hex: ${hex}`);
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function hslToRgb(h: number, s: number, l: number): [number, number, number] {
  const k = (n: number) => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return [f(0) * 255, f(8) * 255, f(4) * 255];
}

/** sRGB channels 0–255 plus alpha 0–1. */
type Rgba = [number, number, number, number];

/** SPEC.md "Contrast": WCAG 2 relative luminance. */
function luminance([r, g, b]: Rgba): number {
  const lin = (c: number) => {
    const v = c / 255;
    return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function contrast(a: Rgba, b: Rgba): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** Paint `top` over an opaque `bottom` (source-over). */
function over(top: Rgba, bottom: Rgba): Rgba {
  const a = top[3];
  return [0, 1, 2].map((i) => top[i] * a + bottom[i] * (1 - a)).concat(1) as Rgba;
}

/** Split a CSS argument list on top-level commas. */
function splitArgs(body: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < body.length; i++) {
    if (body[i] === '(') depth++;
    else if (body[i] === ')') depth--;
    else if (body[i] === ',' && depth === 0) {
      out.push(body.slice(start, i).trim());
      start = i + 1;
    }
  }
  out.push(body.slice(start).trim());
  return out;
}

/**
 * Resolve a custom property to a colour for one theme, the way the browser
 * would: `var()` chains, hex, `transparent`, and `color-mix(in srgb, …)` with
 * premultiplied alpha. Anything else (gradients, HSL channel lists, lengths)
 * is not a colour and resolves to null.
 */
function makeResolver(props: Map<string, string>) {
  const resolveValue = (value: string, seen: Set<string>): Rgba | null => {
    const v = value.trim();
    if (v === 'transparent') return [0, 0, 0, 0];
    const hex = v.match(/^#([0-9a-f]{6})$/i);
    if (hex) return [...parseHex(v), 1];
    const ref = v.match(/^var\((--[\w-]+)\)$/);
    if (ref) return resolveName(ref[1], seen);
    const mix = v.match(/^color-mix\(in srgb,\s*(.*)\)$/s);
    if (mix) {
      const parts = splitArgs(mix[1]).map((part) => {
        const m = part.match(/^(.*?)(?:\s+([\d.]+)%)?$/s)!;
        return { colour: m[1], pct: m[2] === undefined ? null : Number(m[2]) / 100 };
      });
      if (parts.length !== 2) return null;
      const [p, q] = parts;
      const pp = p.pct ?? (q.pct === null ? 0.5 : 1 - q.pct);
      const qp = q.pct ?? 1 - pp;
      const c1 = resolveValue(p.colour, seen);
      const c2 = resolveValue(q.colour, seen);
      if (!c1 || !c2) return null;
      const alpha = c1[3] * pp + c2[3] * qp;
      if (alpha === 0) return [0, 0, 0, 0];
      const ch = [0, 1, 2].map((i) => (c1[i] * c1[3] * pp + c2[i] * c2[3] * qp) / alpha);
      return [ch[0], ch[1], ch[2], alpha];
    }
    return null;
  };
  const resolveName = (name: string, seen: Set<string>): Rgba | null => {
    if (seen.has(name)) throw new Error(`cycle through ${name}`);
    const value = props.get(name);
    if (value === undefined) throw new Error(`undeclared ${name}`);
    return resolveValue(value, new Set([...seen, name]));
  };
  return (name: string) => resolveName(name, new Set());
}

async function compileUtilities(
  classes: string[],
  opts: { themeInline?: string; extras?: string; importDefaultTheme?: boolean } = {},
): Promise<string> {
  const { compile } = await import('tailwindcss');
  const twRoot = resolve(WEB_SRC, '../node_modules/tailwindcss');
  const css = `
@layer theme, base, components, utilities;
${opts.importDefaultTheme ? '@import "tailwindcss/theme.css" layer(theme);' : ''}
@layer theme {
  @theme inline {
${opts.themeInline ?? ''}
  }
}
${opts.extras ?? ''}
@source inline("${classes.join(' ')}");
@import "tailwindcss/utilities.css" source(none);
`;
  const { build } = await compile(css, {
    base: WEB_SRC,
    loadStylesheet: async (id) => {
      const file =
        id === 'tailwindcss/utilities.css'
          ? 'utilities.css'
          : id === 'tailwindcss/theme.css'
            ? 'theme.css'
            : null;
      if (!file) throw new Error(`unexpected stylesheet ${id}`);
      const path = join(twRoot, file);
      return { path, base: twRoot, content: readFileSync(path, 'utf8') };
    },
  });
  return build(classes);
}
