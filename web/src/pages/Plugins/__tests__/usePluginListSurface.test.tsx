import { describe, it, expect } from 'vitest';
import { act } from '@testing-library/react';
import { useLocation } from 'react-router';
import { renderHookWithProviders } from '@/test/utils';
import { usePluginListSurface, type ListSort } from '../hooks/usePluginListSurface';

/**
 * The list model's narrowing and order are the address bar's. These read the
 * two back the way a reload or a shared link would, and check that pressing a
 * pill or picking an order writes them again — the tabs only render what this
 * returns.
 */

interface Row {
  id: string;
  on: boolean;
  attention: boolean;
  stamp: number | null;
}

// Deliberately not in name order: "recommended" is the order handed in, so a
// test that expected the same order for both would prove neither.
const ROWS: Row[] = [
  { id: 'echo', on: true, attention: true, stamp: 5 },
  { id: 'alpha', on: true, attention: false, stamp: 1 },
  { id: 'foxtrot', on: false, attention: false, stamp: 4 },
  { id: 'bravo', on: false, attention: true, stamp: 3 },
  { id: 'delta', on: false, attention: false, stamp: null },
  { id: 'charlie', on: true, attention: false, stamp: 2 },
];

const RECOMMENDED = ['echo', 'alpha', 'foxtrot', 'bravo', 'delta', 'charlie'];
const BY_NAME = ['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot'];

const SORT: ListSort<Row> = {
  order: (a, b, key) => {
    if (key === 'name') return a.id.localeCompare(b.id);
    if (key === 'recent') return (b.stamp ?? -1) - (a.stamp ?? -1) || a.id.localeCompare(b.id);
    return 0;
  },
};

function renderSurface(route: string, rows: readonly Row[] = ROWS) {
  return renderHookWithProviders(
    () => {
      const surface = usePluginListSurface<Row>({
        rows,
        matches: (row, query) => row.id.includes(query),
        key: (row) => row.id,
        name: (row) => row.id,
        isOn: (row) => row.on,
        needsUser: (row) => row.attention,
        justAdded: 'plugin',
        startWhen: false,
        sort: SORT,
      });
      return { surface, search: new URLSearchParams(useLocation().search) };
    },
    { route },
  );
}

const ids = (rows: readonly Row[]) => rows.map((row) => row.id);

describe('the state pill', () => {
  it('narrows the list a link lands on', () => {
    const { result } = renderSurface('/plugins?tab=plugins&state=off');
    expect(result.current.surface.stateFilter).toBe('off');
    expect(ids(result.current.surface.visible)).toEqual(['foxtrot', 'bravo', 'delta']);
  });

  it('writes the pressed pill to the address and drops it for all', () => {
    const { result } = renderSurface('/plugins?tab=plugins');
    expect(ids(result.current.surface.visible)).toHaveLength(ROWS.length);

    act(() => result.current.surface.controls?.onStateFilterChange('on'));
    expect(result.current.search.get('state')).toBe('on');
    expect(ids(result.current.surface.visible)).toEqual(['echo', 'alpha', 'charlie']);

    act(() => result.current.surface.controls?.onStateFilterChange('attention'));
    expect(result.current.surface.stateFilter).toBe('attention');
    expect(ids(result.current.surface.visible)).toEqual(['echo', 'bravo']);

    act(() => result.current.surface.controls?.onStateFilterChange('all'));
    expect(result.current.search.has('state')).toBe(false);
    expect(ids(result.current.surface.visible)).toHaveLength(ROWS.length);
  });

  it('counts what the search leaves, so each number is what its pill shows', () => {
    const { result } = renderSurface('/plugins?tab=plugins&q=a');
    const counts = result.current.surface.controls?.counts;
    expect(counts).toBeDefined();
    const searched = ROWS.filter((row) => row.id.includes('a'));
    expect(counts?.all).toBe(searched.length);
    expect(counts?.on).toBe(searched.filter((row) => row.on).length);
    expect(counts?.off).toBe(searched.filter((row) => !row.on).length);
    expect(counts?.attention).toBe(searched.filter((row) => row.attention).length);
  });

  it('keeps a pill narrowing a short list on screen to be undone', () => {
    const short = ROWS.slice(0, 2);
    expect(renderSurface('/plugins?tab=plugins', short).result.current.surface.controls).toBeNull();
    expect(
      renderSurface('/plugins?tab=plugins&state=off', short).result.current.surface.controls,
    ).not.toBeNull();
  });
});

describe('the order', () => {
  it('reads the chosen order back from the address', () => {
    const { result } = renderSurface('/plugins?tab=plugins&sort=name');
    expect(result.current.surface.sort).toBe('name');
    expect(result.current.surface.controls?.sort?.value).toBe('name');
    expect(ids(result.current.surface.visible)).toEqual(BY_NAME);
  });

  it('orders by recency, undated rows last', () => {
    const { result } = renderSurface('/plugins?tab=plugins&sort=recent');
    expect(ids(result.current.surface.visible)).toEqual([
      'echo',
      'foxtrot',
      'bravo',
      'charlie',
      'alpha',
      'delta',
    ]);
  });

  it('writes the chosen order and drops the default back to absent', () => {
    const { result } = renderSurface('/plugins?tab=plugins');
    act(() => result.current.surface.controls?.sort?.onChange('recent'));
    expect(result.current.search.get('sort')).toBe('recent');
    expect(result.current.surface.sort).toBe('recent');

    act(() => result.current.surface.controls?.sort?.onChange('recommended'));
    expect(result.current.search.has('sort')).toBe(false);
    expect(result.current.surface.sort).toBe('recommended');
    expect(ids(result.current.surface.visible)).toEqual(RECOMMENDED);
  });

  it('ignores an order this list does not offer', () => {
    const { result } = renderSurface('/plugins?tab=plugins&sort=popular');
    expect(result.current.surface.sort).toBe('recommended');
  });
});

describe('reset', () => {
  it('drops the search and the pill, and leaves the order it was read in', () => {
    const { result } = renderSurface('/plugins?tab=plugins&q=alpha&state=on&sort=name');
    expect(result.current.surface.narrowed).toBe(true);

    act(() => result.current.surface.reset());
    expect(result.current.search.has('q')).toBe(false);
    expect(result.current.search.has('state')).toBe(false);
    expect(result.current.search.get('sort')).toBe('name');
    expect(result.current.surface.narrowed).toBe(false);
    expect(ids(result.current.surface.visible)).toHaveLength(ROWS.length);
  });
});
