import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, Compass, Plus, RefreshCw, Search, X } from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { HeaderButton } from '@/components/mcp/McpPrimitives';
import type { SectionView } from '../../utils/detailParam';
import type { AddIntent } from '../../utils/addParam';
import {
  ADD_ROUTES,
  hasAdd,
  hasDiscover,
  SECTION_DEFINITION_KEYS,
  SECTION_LABEL_KEYS,
  type AddSection,
  type Section,
} from './sections';

const SEARCH_PLACEHOLDER_KEYS: Record<AddSection, string> = {
  plugins: 'plugins.shell.search.plugins',
  mcp: 'plugins.shell.search.mcp',
  skills: 'plugins.shell.search.skills',
  subagents: 'plugins.shell.search.subagents',
};

/**
 * A section's header: its title with the Yours / Discover switch beside it,
 * the status line under it, and search, refresh and Add on the right. Hidden
 * while a detail page is open, which carries its own breadcrumb and header.
 * With no catalog there is only Yours, so neither the switch nor Add leads to
 * one.
 *
 * Refresh is the page's one manual re-read: the lists are cached for a while
 * (a rail switch must not spend a request), so the button is how a user asks
 * again after changing something elsewhere.
 */
export function PageHeader({
  section,
  view,
  summary,
  query,
  hasCatalog,
  refreshing,
  onRefresh,
  onViewChange,
  onQueryChange,
  onAdd,
}: {
  section: Section;
  view: SectionView;
  summary: ReactNode;
  query: string;
  /** The server has a catalog for Discover to list. */
  hasCatalog: boolean;
  /** A manual refresh is in flight; the button holds and spins. */
  refreshing: boolean;
  onRefresh: () => void;
  onViewChange: (view: SectionView) => void;
  onQueryChange: (query: string) => void;
  onAdd: (intent: AddIntent) => void;
}) {
  const { t } = useTranslation();
  // Search and Add belong to the sections that list what the agent uses.
  // An Accounts section adds from inside its own list, and an Add above it
  // that installs something else reads as the same button twice.
  const installs = hasAdd(section);
  // A section the catalog does not carry yet has no Discover side to switch
  // to or to browse from.
  const catalog = hasCatalog && hasDiscover(section);
  const routes = installs ? ADD_ROUTES[section] : [];
  return (
    <header className="plugins-head">
      <div className="plugins-head-text">
        <div className="plugins-head-row">
          <div className="plugins-head-copy">
            <span className="plugins-head-eyebrow">CONDUCTOR / ASSETS</span>
            <h1 className="plugins-head-title">{t(SECTION_LABEL_KEYS[section])}</h1>
          </div>
          {catalog && <ViewSwitch value={view} onChange={onViewChange} />}
        </div>
        <p className="plugins-head-def">{t(SECTION_DEFINITION_KEYS[section])}</p>
        {summary}
      </div>
      <div className="plugins-head-actions">
        <HeaderButton
          variant="secondary"
          className="plugins-head-refresh"
          disabled={refreshing}
          title={t(refreshing ? 'plugins.refresh.refreshing' : 'plugins.refresh.label')}
          aria-label={t(refreshing ? 'plugins.refresh.refreshing' : 'plugins.refresh.label')}
          onClick={onRefresh}
        >
          <RefreshCw
            className={refreshing ? 'plugins-head-refresh-icon is-spinning' : 'plugins-head-refresh-icon'}
            aria-hidden
          />
        </HeaderButton>
        {installs && (
          <SearchField
            value={query}
            placeholder={t(
              view === 'discover' ? 'plugins.shell.search.catalog' : SEARCH_PLACEHOLDER_KEYS[section],
            )}
            onChange={onQueryChange}
          />
        )}
        {installs && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <HeaderButton variant="primary" icon={Plus} className="plugins-head-add">
                {t('plugins.addMenu.add')}
                <ChevronDown className="h-3 w-3 opacity-70" />
              </HeaderButton>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {/* The catalog first: most of what is worth adding is already
                  in it, set up and checked. Already on it, the item would
                  go nowhere. */}
              {catalog && view !== 'discover' && (
                <>
                  <DropdownMenuItem onSelect={() => onViewChange('discover')}>
                    <Compass className="h-3.5 w-3.5" />
                    {t('plugins.addMenu.browseCatalog')}
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                </>
              )}
              {routes.map((route) => (
                <DropdownMenuItem
                  key={route.intent}
                  className={route.hintKey ? 'plugins-add-item' : undefined}
                  onSelect={() => onAdd(route.intent)}
                >
                  {route.icon && <route.icon className="h-3.5 w-3.5" />}
                  {route.hintKey ? (
                    <span className="plugins-add-text">
                      <span>{t(route.key)}</span>
                      <span className="plugins-add-hint">{t(route.hintKey)}</span>
                    </span>
                  ) : (
                    t(route.key)
                  )}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
    </header>
  );
}

/**
 * Yours or Discover. The house SegmentedControl presses its segment in amber,
 * and amber on this page is annotation only, so this is the same pressed
 * button group drawn in the neutral elevated fill.
 */
function ViewSwitch({
  value,
  onChange,
}: {
  value: SectionView;
  onChange: (view: SectionView) => void;
}) {
  const { t } = useTranslation();
  const options: { value: SectionView; label: string }[] = [
    { value: 'yours', label: t('plugins.shell.view.yours') },
    { value: 'discover', label: t('plugins.shell.view.discover') },
  ];
  return (
    <div role="group" aria-label={t('plugins.shell.view.label')} className="plugins-view">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={option.value === value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

/**
 * The header search, written to `?q=` so every section filters by one value
 * and a reload keeps it.
 *
 * The field keeps its own text while it has focus and shows the URL's
 * otherwise. Navigation lands in a transition, so a field bound straight to
 * the URL would lag a keystroke behind and drop characters typed in between;
 * once the field lets go, the URL is the truth again, which is what makes a
 * rail click that clears `q` clear the field too.
 */
function SearchField({
  value,
  placeholder,
  onChange,
}: {
  value: string;
  placeholder: string;
  onChange: (value: string) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(value);
  const [focused, setFocused] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const shown = focused ? draft : value;

  // A section is a list to be scanned; "/" puts the caret in its search from
  // anywhere on the page, as it does in the rest of the app.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== '/' || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || target?.isContentEditable) return;
      event.preventDefault();
      inputRef.current?.focus();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  const set = (next: string) => {
    setDraft(next);
    onChange(next);
  };

  return (
    <div className="plugins-search">
      <Search className="plugins-search-icon" aria-hidden />
      <input
        ref={inputRef}
        type="search"
        value={shown}
        placeholder={placeholder}
        aria-label={placeholder}
        onFocus={() => {
          setDraft(value);
          setFocused(true);
        }}
        onBlur={() => setFocused(false)}
        onChange={(e) => set(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape' && shown) {
            e.preventDefault();
            set('');
          }
        }}
      />
      {shown && (
        <button
          type="button"
          className="plugins-search-clear"
          aria-label={t('plugins.shell.search.clear')}
          // Keeps focus in the field, so the cleared draft is the one shown.
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => set('')}
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );
}
