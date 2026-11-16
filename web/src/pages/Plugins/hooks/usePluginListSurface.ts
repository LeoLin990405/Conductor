import { useCallback, useMemo, type ComponentProps } from 'react';
import { useSearchParams } from 'react-router';
import type { QueryClient } from '@tanstack/react-query';
import {
  CONTROLS_MIN_ROWS,
  matchesStateFilter,
  type ListControls,
  type StateCounts,
  type StateFilter,
} from '../components/ListControls';
import type { CellMode } from '../components/surface/Surface';
import {
  useBulkRunner,
  useBulkSelection,
  type BulkRunOptions,
  type BulkSelection,
  type BulkTarget,
} from '../components/useBulkSelection';
import {
  LIST_SORTS,
  readSort,
  readStateFilter,
  SORT_LABEL_KEYS,
  SORT_PARAM,
  STATE_PARAM,
  type ListSortKey,
} from '../utils/listParams';
import { useJustAdded, type JustAddedKind } from './useJustAdded';
import { useOnceTrue } from './useOnceTrue';

/**
 * The list model every Plugins tab reads: the header's search (`?q=`), the
 * state pills and their counts (`?state=`), the order the rows read in
 * (`?sort=`, where the tab offers one), select mode with its bulk runner, the
 * rows all of that leaves on screen, and the verdicts that depend on them
 * (must a group stay unfolded, has a narrowed list anything left to show, is
 * the first-run block up), plus the one call that drops the narrowing again.
 *
 * One narrowing question, answered once over the tab's WHOLE population: when
 * a section answered it from its own rows while the groups below it were
 * filtered independently, filtering by a plugin's name printed "No matches"
 * directly above the group that had matched.
 *
 * The kind of list does not change the controls: every tab filters by the same
 * states and, where it has an order of its own, sorts by the same vocabulary,
 * so the two params are read here once. What is derived from them is memoised:
 * a tab re-renders on every query the page shell reads, and re-sorting dozens
 * of rows on each of those is work nobody asked for.
 */

export interface PluginListSurface {
  /** The header search, trimmed. */
  filter: string;
  stateFilter: StateFilter;
  selection: BulkSelection;
  progress: { done: number; total: number } | null;
  run: <R>(targets: BulkTarget<R>[], opts: BulkRunOptions<R>) => void;
  /** A search term or a state pill is narrowing the list. */
  narrowed: boolean;
  /** Drop both narrowing controls at once, the way the empty state offers. */
  reset: () => void;
}

/** What a folding `SurfaceGrid` takes. */
export interface GridFold {
  rows: number | undefined;
  hiddenNames: string[];
  reveal: boolean;
}

/** A tab's own reading order, where it offers more than one. */
export interface ListSort<T> {
  /** The orders offered; the first is the default and is not written to the
   *  URL, so a link without `?sort` reads as the tab intended. */
  keys?: readonly ListSortKey[];
  /** Negative/positive as `a` comes before/after `b` in `key`. */
  order: (a: T, b: T, key: ListSortKey) => number;
}

export interface PluginList<T> extends PluginListSurface {
  /** The rows the search and the state pill leave, in the order chosen. */
  visible: T[];
  /** The order in force ('recommended' where the tab offers none). */
  sort: ListSortKey;
  cellMode: (row: T) => CellMode;
  /**
   * A group folded after `rows` rows, unfolded whole while the list is
   * narrowed or being selected from, and for good once it holds what was just
   * added, so the new cell is on screen and stays there after its mark fades.
   */
  fold: (rows: number, names: readonly string[]) => GridFold;
  /** The first-run catalog block is up. */
  showStart: boolean;
  /** The narrowing left nothing on screen. */
  nothingMatches: boolean;
  /** The pills, the order and Select, or null where they are furniture. */
  controls: ComponentProps<typeof ListControls> | null;
}

export function usePluginListSurface<T>(opts: {
  rows: readonly T[];
  /** The header search over one row. */
  matches: (row: T, query: string) => boolean;
  key: (row: T) => string;
  /** What the just-added mark is keyed by. */
  name: (row: T) => string;
  isOn: (row: T) => boolean;
  /** Absent on a tab whose rows never need the user, which keeps that pill
   *  at zero and so hidden. */
  needsUser?: (row: T) => boolean;
  justAdded: JustAddedKind;
  /**
   * Nothing of the user's yet, so the catalog comes first. Held once true,
   * since an install from it lands in this same list and would otherwise
   * take the block, and the install's own dialogs, away mid-flow.
   */
  startWhen: boolean;
  /** The tab's invalidation radius after a bulk run (default: plugin-wide). */
  invalidate?: (qc: QueryClient) => void;
  /** Present when the tab reads in more than one order. */
  sort?: ListSort<T>;
}): PluginList<T> {
  const { rows, key, name, isOn } = opts;
  const [searchParams, setSearchParams] = useSearchParams();
  const selection = useBulkSelection();
  const { progress, run } = useBulkRunner(selection, opts.invalidate);
  const isJustAdded = useJustAdded();
  const startSeen = useOnceTrue(opts.startWhen);

  // Defaults are module constants (or absent), so the memo below does not see
  // a fresh function identity on every render.
  const matches = opts.matches;
  const needsUser = opts.needsUser ?? never;
  const sortOrder = opts.sort?.order;
  const sortKeys = opts.sort?.keys ?? LIST_SORTS;
  const defaultSort = sortKeys[0] ?? 'recommended';

  const filter = (searchParams.get('q') ?? '').trim();
  const stateFilter = readStateFilter(searchParams);
  const sort = sortOrder ? readSort(searchParams, sortKeys) : 'recommended';
  const narrowed = !!filter || stateFilter !== 'all';
  const forceExpanded = selection.selecting || narrowed;
  const showStart = startSeen && !narrowed;

  const setParam = useCallback(
    (param: string, value: string | null) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (value === null) next.delete(param);
          else next.set(param, value);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const onStateFilterChange = useCallback(
    (next: StateFilter) => setParam(STATE_PARAM, next === 'all' ? null : next),
    [setParam],
  );
  const onSortChange = useCallback(
    (next: ListSortKey) => setParam(SORT_PARAM, next === defaultSort ? null : next),
    [setParam, defaultSort],
  );

  const ordered = useMemo(() => {
    if (!sortOrder || sort === 'recommended') return rows;
    return [...rows].sort((a, b) => sortOrder(a, b, sort));
  }, [rows, sortOrder, sort]);

  // The pills count what the search leaves, so each number is what pressing
  // that pill would show.
  const queried = useMemo(
    () => ordered.filter((row) => matches(row, filter)),
    [ordered, matches, filter],
  );
  const counts = useMemo<StateCounts>(() => {
    const onCount = queried.filter(isOn).length;
    return {
      all: queried.length,
      on: onCount,
      off: queried.length - onCount,
      attention: queried.filter(needsUser).length,
    };
  }, [queried, isOn, needsUser]);
  const visible = useMemo(
    () => queried.filter((row) => matchesStateFilter(stateFilter, isOn(row), needsUser(row))),
    [queried, stateFilter, isOn, needsUser],
  );

  // Select mode works on the rows on screen.
  const shownKeys = useMemo(() => visible.map(key), [visible, key]);
  const allShownSelected =
    shownKeys.length > 0 && shownKeys.every((k) => selection.selected.has(k));

  const reset = useCallback(() => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete('q');
        next.delete(STATE_PARAM);
        return next;
      },
      { replace: true },
    );
  }, [setSearchParams]);

  return {
    filter,
    stateFilter,
    selection,
    progress,
    run,
    narrowed,
    reset,
    visible,
    sort,
    cellMode: (row) => ({
      select: selection.selecting
        ? { checked: selection.selected.has(key(row)), onToggle: () => selection.toggle(key(row)) }
        : undefined,
      justAdded: isJustAdded(opts.justAdded, name(row)),
    }),
    fold: (limit, names) => ({
      rows: forceExpanded ? undefined : limit,
      hiddenNames: [...names],
      reveal: names.some((n) => isJustAdded(opts.justAdded, n)),
    }),
    showStart,
    nothingMatches: narrowed && visible.length === 0,
    // Not on a first visit: the catalog is the point there. A short list is
    // read whole, but a pill or an order still narrowing it stays to be
    // undone: hidden, it would go on filtering with nothing on screen to
    // clear it.
    controls:
      showStart ||
      (rows.length < CONTROLS_MIN_ROWS && stateFilter === 'all' && sort === defaultSort)
        ? null
        : {
            counts,
            stateFilter,
            onStateFilterChange,
            sort: sortOrder
              ? {
                  value: sort,
                  options: sortKeys.map((sortKey) => ({
                    key: sortKey,
                    labelKey: SORT_LABEL_KEYS[sortKey],
                  })),
                  onChange: onSortChange,
                }
              : undefined,
            selecting: selection.selecting,
            onStartSelect: selection.start,
            shownCount: shownKeys.length,
            allShownSelected,
            onSelectShown: (on: boolean) => selection.setMany(shownKeys, on),
          },
  };
}

/** The absent `needsUser`: no row ever needs the user. */
function never(): boolean {
  return false;
}
