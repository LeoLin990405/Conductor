import type { StateFilter } from '../components/ListControls';

/**
 * The list controls that survive a reload: the state pill a Yours list is
 * narrowed to (`?state=off`) and the order it is read in (`?sort=name`).
 *
 * Both used to be component state, which made a narrowed list impossible to
 * share or come back to: a link into `?tab=skills&q=recap` reopened on all
 * skills, and a rail click lost the pill with no way to name it again. The
 * search was already in the address; these two follow it.
 *
 * `recommended` is the order the tab chooses for itself (what needs the user
 * first, then the tab's own reading order), so it is the default and is not
 * written to the URL: a link without `?sort` reads as the tab intended.
 */

export const STATE_PARAM = 'state';
export const SORT_PARAM = 'sort';

/** In the order the pills draw them. */
export const STATE_FILTERS: readonly StateFilter[] = ['all', 'on', 'off', 'attention'];

export function isStateFilter(value: string | null | undefined): value is StateFilter {
  return !!value && (STATE_FILTERS as readonly string[]).includes(value);
}

/** The pill the URL names, or 'all' for an absent or unknown value. */
export function readStateFilter(params: URLSearchParams): StateFilter {
  const value = params.get(STATE_PARAM);
  return isStateFilter(value) ? value : 'all';
}

/** The orders a list offers, first is its default. */
export type ListSortKey = 'recommended' | 'name' | 'recent';

export const LIST_SORTS: readonly ListSortKey[] = ['recommended', 'name', 'recent'];

/** The label of each order. Literal keys so the locale parity test sees each
 *  one; the order is the map's own key order, which `LIST_SORTS` restates. */
export const SORT_LABEL_KEYS: Record<ListSortKey, string> = {
  recommended: 'plugins.sort.recommended',
  name: 'plugins.sort.name',
  recent: 'plugins.sort.recent',
};

export function isListSortKey(
  value: string | null | undefined,
  allowed: readonly ListSortKey[] = LIST_SORTS,
): value is ListSortKey {
  return !!value && (allowed as readonly string[]).includes(value);
}

/**
 * The order the URL names, or the list's default. An unknown value falls back
 * to the default rather than sorting by nothing: the URL may outlive the list
 * that wrote it.
 */
export function readSort(
  params: URLSearchParams,
  allowed: readonly ListSortKey[] = LIST_SORTS,
): ListSortKey {
  const value = params.get(SORT_PARAM);
  return isListSortKey(value, allowed) ? value : (allowed[0] ?? 'recommended');
}
