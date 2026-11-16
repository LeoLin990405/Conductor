import { useTranslation } from 'react-i18next';
import { ArrowUpDown, Check, ListChecks } from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { cn } from '@/lib/utils';
import type { ListSortKey } from '../utils/listParams';

/**
 * The row under a list's header, shared by Plugins, Connectors and Skills:
 * the state pills with their counts, the order the list reads in where the
 * list offers one, and the way into select mode. The search is the page
 * header's, so this row only narrows by state and sorts. A non-'all' pill
 * unfolds every group it leaves rows in; Select turns the cells into
 * checkboxes under the BulkActionBar.
 *
 * The pills and the order are the URL's (`?state=`, `?sort=`), which is what
 * makes a narrowed list a link: the tab reads them back on reload, and a
 * detail page opened over it leaves them alone.
 */

export type StateFilter = 'all' | 'on' | 'off' | 'attention';

export type StateCounts = Record<StateFilter, number>;

/** The order control: what the list is sorted by, and the orders on offer. */
export interface ListSortControl {
  value: ListSortKey;
  options: readonly { key: ListSortKey; labelKey: string }[];
  onChange: (key: ListSortKey) => void;
}

/** Below this many rows a list is read whole, and pills or a Select over it
 *  are furniture. */
export const CONTROLS_MIN_ROWS = 6;

/** The shared predicate behind the state pills. `attention` is the row's own
 *  definition of needing a human (broken OAuth, missing secret). */
export function matchesStateFilter(
  stateFilter: StateFilter,
  enabled: boolean,
  attention = false,
): boolean {
  switch (stateFilter) {
    case 'on':
      return enabled;
    case 'off':
      return !enabled;
    case 'attention':
      return attention;
    default:
      return true;
  }
}

export function ListControls({
  counts,
  stateFilter,
  onStateFilterChange,
  sort,
  selecting,
  onStartSelect,
  shownCount,
  allShownSelected,
  onSelectShown,
}: {
  /** Per pill, over the rows the search leaves. */
  counts: StateCounts;
  stateFilter: StateFilter;
  onStateFilterChange: (value: StateFilter) => void;
  /** Present on a list that offers more than one order. */
  sort?: ListSortControl;
  selecting: boolean;
  onStartSelect: () => void;
  /** The rows on screen, which "Select all" chooses. */
  shownCount: number;
  allShownSelected: boolean;
  onSelectShown: (on: boolean) => void;
}) {
  const { t } = useTranslation();
  // A search that left nothing has nothing to narrow or select; the empty
  // state under it carries the way back.
  if (counts.all === 0 && stateFilter === 'all' && !selecting) return null;
  const pills: { key: StateFilter; label: string }[] = [
    { key: 'all', label: t('plugins.stateFilter.all') },
    { key: 'on', label: t('plugins.stateFilter.on') },
    { key: 'off', label: t('plugins.stateFilter.off') },
    { key: 'attention', label: t('plugins.stateFilter.attention') },
  ];
  // A pill that would show nothing is not offered, unless it is the one the
  // list is narrowed to, which has to stay to be undone.
  const shown = pills.filter((p) => p.key === 'all' || counts[p.key] > 0 || p.key === stateFilter);
  const currentSort = sort?.options.find((option) => option.key === sort.value) ?? sort?.options[0];
  return (
    <div className="list-controls">
      <div role="group" aria-label={t('plugins.stateFilter.label')} className="list-controls-pills">
        {shown.map((pill) => (
          <button
            key={pill.key}
            type="button"
            aria-pressed={stateFilter === pill.key}
            className={cn('surface-chip', stateFilter === pill.key && 'is-on')}
            onClick={() => onStateFilterChange(pill.key)}
          >
            {pill.label}
            <span className="surface-count">{counts[pill.key]}</span>
          </button>
        ))}
      </div>
      <div className="list-controls-side">
        {sort && currentSort && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                className="list-controls-btn"
                aria-label={t('plugins.sort.aria', { value: t(currentSort.labelKey) })}
              >
                <ArrowUpDown className="h-3.5 w-3.5" aria-hidden />
                {t(currentSort.labelKey)}
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-[160px]">
              {sort.options.map((option) => {
                const active = option.key === sort.value;
                return (
                  <DropdownMenuCheckboxItem
                    key={option.key}
                    checked={active}
                    onSelect={() => {
                      if (!active) sort.onChange(option.key);
                    }}
                  >
                    <Check
                      className="h-3.5 w-3.5"
                      style={{ opacity: active ? 1 : 0 }}
                      aria-hidden
                    />
                    {t(option.labelKey)}
                  </DropdownMenuCheckboxItem>
                );
              })}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
        {selecting ? (
          <button
            type="button"
            className="list-controls-btn"
            disabled={shownCount === 0}
            onClick={() => onSelectShown(!allShownSelected)}
          >
            {allShownSelected
              ? t('plugins.bulk.selectNone')
              : t('plugins.bulk.selectAll', { count: shownCount })}
          </button>
        ) : (
          <button type="button" className="list-controls-btn" onClick={onStartSelect}>
            <ListChecks className="h-3.5 w-3.5" aria-hidden />
            {t('plugins.bulk.select')}
          </button>
        )}
      </div>
    </div>
  );
}
