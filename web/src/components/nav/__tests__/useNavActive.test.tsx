/**
 * Active-state matrix for the shared nav config:
 *  - every item is active at its exact path
 *  - 'exact-or-sub' items are active at sub-paths (key + '/...') only
 *  - the 'prefix' item (/chat) is active at any deeper path
 *  - sibling items never light up for each other's routes
 *
 * Matching, not visibility: NAV_ITEMS is every route the nav knows, and which
 * of them a given user is shown is `useNavItems`' answer (its own suite). A
 * hidden item is still the active one when the user is standing on its route.
 */
import React from 'react';
import { describe, it, expect } from 'vitest';
import { renderHook } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { NAV_ITEMS, SETTINGS_ITEM } from '../navItems';
import { useNavActive } from '../useNavActive';

const ALL_ITEMS = [...NAV_ITEMS, SETTINGS_ITEM];

function matcherAt(pathname: string) {
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <MemoryRouter initialEntries={[pathname]}>{children}</MemoryRouter>
  );
  const { result } = renderHook(() => useNavActive(), { wrapper });
  return result.current;
}

describe('useNavActive', () => {
  it.each(ALL_ITEMS.map((item) => [item.key, item] as const))(
    'marks %s active at its exact path',
    (_key, item) => {
      expect(matcherAt(item.key)(item)).toBe(true);
    },
  );

  it.each(
    ALL_ITEMS.filter((item) => item.match === 'exact-or-sub').map(
      (item) => [item.key, item] as const,
    ),
  )('marks exact-or-sub item %s active at a sub-path', (key, item) => {
    expect(matcherAt(`${key}/details`)(item)).toBe(true);
  });

  it('has no prefix item in the Super-agent shell', () => {
    expect(NAV_ITEMS.some((item) => item.match === 'prefix')).toBe(false);
  });

  it.each(ALL_ITEMS.map((item) => [item.key, item] as const))(
    'does not mark siblings active at %s',
    (key, item) => {
      const isActive = matcherAt(key);
      for (const sibling of ALL_ITEMS) {
        if (sibling === item) continue;
        expect(isActive(sibling)).toBe(false);
      }
    },
  );

  it('does not match a route that merely shares the key as a string prefix (exact-or-sub)', () => {
    const tasks = NAV_ITEMS.find((item) => item.key === '/tasks')!;
    expect(matcherAt('/tasks-archive')(tasks)).toBe(false);
  });
});
