import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import i18n from '@/i18n';

const links = vi.hoisted(() => ({ PRIVACY_URL: 'https://example.test/privacy' as string | null }));
vi.mock('../../../config/supportLinks', () => ({
  get PRIVACY_URL() {
    return links.PRIVACY_URL;
  },
}));

import Legal, { REPOSITORY_URL } from '../Legal';

const renderLegal = () =>
  render(
    <MemoryRouter>
      <Legal />
    </MemoryRouter>,
  );

afterEach(async () => {
  links.PRIVACY_URL = 'https://example.test/privacy';
  await act(async () => {
    await i18n.changeLanguage('en-US');
  });
});

describe('Legal (terminal-native)', () => {
  it('renders the tnLegal copy in en-US with every link translated', () => {
    renderLegal();
    expect(screen.getByRole('heading', { level: 1, name: 'Legal & Attribution' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: 'Other credits' })).toBeInTheDocument();
    const back = screen.getByRole('link', { name: /Back to Conductor/ });
    expect(back).toHaveAttribute('href', '/');
    const repo = screen.getByRole('link', { name: 'Conductor repository' });
    expect(repo).toHaveAttribute('href', REPOSITORY_URL);
    expect(repo).toHaveAttribute('target', '_blank');
    expect(repo).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getByRole('link', { name: 'Privacy Policy' })).toHaveAttribute(
      'href',
      'https://example.test/privacy',
    );
  });

  it('renders zh-CN, including the link text that used to be hardcoded English', async () => {
    await act(async () => {
      await i18n.changeLanguage('zh-CN');
    });
    renderLegal();
    expect(screen.getByRole('heading', { level: 1, name: '法律与署名' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /返回 Conductor/ })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Conductor 仓库' })).toHaveAttribute('href', REPOSITORY_URL);
    expect(screen.getByRole('link', { name: '隐私政策' })).toBeInTheDocument();
    expect(screen.queryByText(/Conductor repository|See also our/)).toBeNull();
  });

  it('omits the privacy paragraph when no privacy URL is configured', () => {
    links.PRIVACY_URL = null;
    renderLegal();
    expect(screen.queryByRole('link', { name: 'Privacy Policy' })).toBeNull();
    expect(screen.getAllByRole('link')).toHaveLength(2);
  });

  it('uses the terminal-native classes for surfaces, prose, mono headings and links', () => {
    renderLegal();
    const root = screen.getByTestId('legal-page');
    expect(root).toHaveClass('tn-legal');
    expect(screen.getByRole('heading', { level: 1 })).toHaveClass('tn-legal-title');
    expect(screen.getByRole('heading', { level: 2 })).toHaveClass('tn-legal-heading');
    for (const a of root.querySelectorAll('a')) {
      expect(a.className).toMatch(/tn-legal-(link|back)/);
    }
  });
});

describe('legal.css', () => {
  const css = readFileSync(resolve(__dirname, '../legal.css'), 'utf8');
  const tokensJson = readFileSync(
    resolve(__dirname, '../../../../../docs/design/terminal-native/system/tokens.json'),
    'utf8',
  );
  const TOKEN_HEX = new Set([...tokensJson.matchAll(/#[0-9a-fA-F]{6}\b/g)].map((m) => m[0].toLowerCase()));

  it('uses only token hexes', () => {
    const used = [...css.matchAll(/#[0-9a-fA-F]{3,8}\b/g)].map((m) => m[0].toLowerCase());
    expect(used.length).toBeGreaterThan(0);
    expect(used.filter((h) => !TOKEN_HEX.has(h))).toEqual([]);
  });

  it('sets prose body, mono headings and the 2px ink.focus ring on links', () => {
    expect(css).toMatch(/\.tn-legal\s*\{[^}]*font-family:\s*var\(--font-prose\)/);
    expect(css).toMatch(/\.tn-legal-title,\s*\.tn-legal-heading\s*\{[^}]*font-family:\s*var\(--font-mono\)/);
    expect(css).toMatch(
      /\.tn-legal a:focus-visible\s*\{[^}]*outline:\s*var\(--border-focus, 2px\) solid var\(--ink-focus/,
    );
    expect(css).toMatch(/\.tn-legal-article\s*\{[^}]*max-width:\s*68ch/);
  });

  it('keeps every radius at or below radius.card', () => {
    const radii = [...css.matchAll(/border-radius:\s*([^;]+);/g)].map((m) => m[1].trim());
    for (const r of radii) {
      expect(['var(--radius-control, 2px)', 'var(--radius-card, 3px)', '0']).toContain(r);
    }
  });
});
