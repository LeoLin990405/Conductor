import { describe, it, expect, vi } from 'vitest';
import type { QueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import { refreshKeys, refreshSection } from '../refresh';

/**
 * Refresh re-reads what the section on screen draws from. The list it mounts
 * is only part of that: a plugin cell's problem is a verdict over the MCP
 * catalog, and Discover's installed marks come from the plugin list, so a
 * refresh that only re-read the mounted list would leave the rest saying what
 * they said before.
 */

function fakeClient() {
  const invalidateQueries = vi.fn(async () => undefined);
  return { invalidateQueries, client: { invalidateQueries } as unknown as Pick<QueryClient, 'invalidateQueries'> };
}

describe('what a refresh re-reads', () => {
  it('reads the section’s own list beside the ones its cells verdict over', () => {
    const keys = refreshKeys('plugins', 'yours').map((key) => [...key].join('/'));
    expect(keys).toContain('plugins');
    expect(keys).toContain('mcp');
  });

  it('reads the catalog and what its cards install in Discover', () => {
    expect(refreshKeys('plugins', 'discover')).toEqual(refreshKeys('mcp', 'discover'));
    const keys = refreshKeys('plugins', 'discover').map((key) => [...key].join('/'));
    expect(keys).toContain('plugins');
    expect(keys).toContain('mcp');
  });

  it('gives every section at least its own list', () => {
    for (const section of ['plugins', 'mcp', 'skills', 'subagents', 'secrets'] as const) {
      expect(refreshKeys(section, 'yours').length).toBeGreaterThan(0);
    }
    expect([...refreshKeys('secrets', 'yours')[0]].join('/')).toBe(
      queryKeys.userVault.all.join('/'),
    );
  });

  it('invalidates each of them once, and settles after all of them', async () => {
    const { invalidateQueries, client } = fakeClient();
    await refreshSection(client, 'plugins', 'yours');

    const keys = refreshKeys('plugins', 'yours');
    expect(invalidateQueries).toHaveBeenCalledTimes(keys.length);
    for (const key of keys) {
      expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: key });
    }
  });
});
