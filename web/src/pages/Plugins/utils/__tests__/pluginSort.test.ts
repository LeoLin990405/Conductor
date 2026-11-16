import { describe, it, expect } from 'vitest';
import type { PluginInfo } from '@/api/core';
import { pluginOrder } from '../pluginSort';

/**
 * "Recently added" is only useful if it means what it says: newest first,
 * and a package the server never dated last rather than first.
 */

function plugin(name: string, over: Partial<PluginInfo> = {}): PluginInfo {
  return {
    name,
    version: null,
    description: '',
    author: null,
    homepage: null,
    source_type: 'git',
    source_ref: 'https://example.test/repo.git',
    enabled: true,
    installed_at: null,
    updated_at: null,
    components: [],
    ...over,
  };
}

const names = (rows: PluginInfo[], key: Parameters<typeof pluginOrder>[2]) =>
  [...rows].sort((a, b) => pluginOrder(a, b, key)).map((row) => row.name);

describe('plugin order', () => {
  it('leaves the recommended reading alone', () => {
    const rows = [plugin('zeta'), plugin('alpha'), plugin('mike')];
    expect(names(rows, 'recommended')).toEqual(['zeta', 'alpha', 'mike']);
  });

  it('sorts by name', () => {
    const rows = [plugin('zeta'), plugin('alpha'), plugin('mike')];
    expect(names(rows, 'name')).toEqual(['alpha', 'mike', 'zeta']);
  });

  it('puts the most recently installed first', () => {
    const rows = [
      plugin('old', { installed_at: '2025-01-01T00:00:00Z' }),
      plugin('new', { installed_at: '2025-06-01T00:00:00Z' }),
      plugin('middle', { installed_at: '2025-03-01T00:00:00Z' }),
    ];
    expect(names(rows, 'recent')).toEqual(['new', 'middle', 'old']);
  });

  it('falls back to the last update when nothing recorded an install', () => {
    const rows = [
      plugin('updated', { updated_at: '2025-05-01T00:00:00Z' }),
      plugin('installed', { installed_at: '2025-02-01T00:00:00Z' }),
    ];
    expect(names(rows, 'recent')).toEqual(['updated', 'installed']);
  });

  it('sorts an undated package last, however it ties', () => {
    const rows = [
      plugin('undated'),
      plugin('dated', { installed_at: '2025-01-01T00:00:00Z' }),
      plugin('bad-date', { installed_at: 'not a date' }),
    ];
    expect(names(rows, 'recent')).toEqual(['dated', 'bad-date', 'undated']);
  });

  it('keeps a stable order between packages with the same date', () => {
    const at = '2025-01-01T00:00:00Z';
    const rows = [plugin('b', { installed_at: at }), plugin('a', { installed_at: at })];
    expect(names(rows, 'recent')).toEqual(['a', 'b']);
  });
});
