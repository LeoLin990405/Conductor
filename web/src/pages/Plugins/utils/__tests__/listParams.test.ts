import { describe, it, expect } from 'vitest';
import {
  LIST_SORTS,
  readSort,
  readStateFilter,
  SORT_LABEL_KEYS,
  STATE_FILTERS,
} from '../listParams';

/**
 * The list controls are the URL's, so what a link says has to be read back
 * exactly: a param a later version stopped honouring must fall back to the
 * list's default rather than narrow it to nothing or sort it by nothing.
 */

describe('the state pill in the address', () => {
  it('reads every pill the control row draws', () => {
    for (const state of STATE_FILTERS) {
      const params = new URLSearchParams({ state });
      expect(readStateFilter(params)).toBe(state);
    }
  });

  it('reads an absent param as all', () => {
    expect(readStateFilter(new URLSearchParams())).toBe('all');
  });

  it('falls back to all for a value no longer offered', () => {
    expect(readStateFilter(new URLSearchParams({ state: 'archived' }))).toBe('all');
    expect(readStateFilter(new URLSearchParams({ state: '' }))).toBe('all');
  });
});

describe('the order in the address', () => {
  it('defaults to the list’s first order', () => {
    expect(readSort(new URLSearchParams())).toBe('recommended');
    expect(readSort(new URLSearchParams({ sort: 'recent' }))).toBe('recent');
  });

  it('falls back to the default for an order the list does not offer', () => {
    expect(readSort(new URLSearchParams({ sort: 'popular' }))).toBe('recommended');
    expect(readSort(new URLSearchParams({ sort: 'recommended' }), ['recent', 'name'])).toBe(
      'recent',
    );
  });

  it('labels every order a list can offer', () => {
    for (const sort of LIST_SORTS) {
      expect(SORT_LABEL_KEYS[sort]).toMatch(/^plugins\.sort\./);
    }
  });
});
