import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import type { AutonomyTicket } from '@/api/autonomy';
import { registerFeatureLocales } from '@/lib/featureLocales';
import { nativeRuntimeLabel } from '@/lib/nativeRuntimeLabels';
import { Age, StateMark, useInfiniteTickets, type TicketStateId } from './shared';
import tnTicketsEn from './locales/tnTickets.en-US.json';
import tnTicketsZh from './locales/tnTickets.zh-CN.json';
import './tasks.css';

// The Tickets list keeps its copy in its own tnTickets pair beside the main
// catalogs; registering at module load makes t('tnTickets.…') resolve.
registerFeatureLocales({ 'en-US': tnTicketsEn, 'zh-CN': tnTicketsZh });

/** The seven terminal-native states, in the order the filter bar lists them. */
const TICKET_STATES: readonly TicketStateId[] = [
  'queued',
  'running',
  'blocked',
  'needs-you',
  'failed',
  'completed',
  'archived',
];

/** Map a Ticket onto one of the seven terminal-native states. */
function ticketState(ticket: Pick<AutonomyTicket, 'status' | 'attention'>): TicketStateId {
  if (ticket.attention) return 'needs-you';
  switch (ticket.status) {
    case 'running':
      return 'running';
    case 'blocked':
      return 'blocked';
    case 'failed':
      return 'failed';
    case 'completed':
      return 'completed';
    case 'cancelled':
    case 'archived':
      return 'archived';
    case 'waiting':
    case 'awaiting_clarification':
      return 'needs-you';
    default:
      return 'queued';
  }
}

/**
 * States that are exactly one backend status: the list asks the backend for
 * that status so paging loads the right rows. The others (queued, needs-you,
 * archived) span several statuses and are filtered over the loaded pages.
 */
const STATE_BACKEND_STATUS: Partial<Record<TicketStateId, string>> = {
  running: 'running',
  blocked: 'blocked',
  failed: 'failed',
  completed: 'completed',
};

const SORTS = ['updated', 'created', 'title'] as const;
type SortId = (typeof SORTS)[number];
const SORT_LABEL: Record<SortId, string> = {
  updated: 'tnTickets.filters.sortUpdated',
  created: 'tnTickets.filters.sortCreated',
  title: 'tnTickets.filters.sortTitle',
};

function isTicketState(value: string): value is TicketStateId {
  return (TICKET_STATES as readonly string[]).includes(value);
}

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.tagName === 'INPUT' ||
    target.tagName === 'TEXTAREA' ||
    target.tagName === 'SELECT' ||
    target.isContentEditable
  );
}

function ticketOrigin(ticket: AutonomyTicket): 'schedule' | 'manual' | 'github' {
  if (ticket.mandate.schedule_id) return 'schedule';
  if (ticket.origin_thread_key.startsWith('manual:')) return 'manual';
  return 'github';
}

function TicketTableRow({
  ticket,
  selected,
  onOpen,
  onSelect,
}: {
  ticket: AutonomyTicket;
  selected: boolean;
  onOpen: () => void;
  onSelect: () => void;
}) {
  const { t } = useTranslation();
  const state = ticketState(ticket);
  const title = ticket.title || ticket.origin_thread_key;
  const provider = ticket.worker_provider ? nativeRuntimeLabel(ticket.worker_provider) : t('tnTickets.workerNone');
  const owner = ticket.worker_daemon_id
    ? t('tnTickets.owner.daemon', { id: ticket.worker_daemon_id })
    : t('tnTickets.owner.cloud');
  return (
    <tr
      className="tn-tix-row"
      data-testid="ticket-row"
      data-ticket-id={ticket.ticket_id}
      data-state={state}
      aria-selected={selected}
      tabIndex={selected ? 0 : -1}
      onClick={onOpen}
      onFocus={onSelect}
    >
      <td className="tn-tix-state" data-label={t('tnTickets.columns.state')}>
        <StateMark state={state} spin={state === 'running'} />
      </td>
      <td className="tn-tix-ticket" data-label={t('tnTickets.columns.ticket')}>
        <div className="tn-tix-title" title={title}>
          {title}
        </div>
        <div className="tn-tix-sub sa-mono-id" title={ticket.ticket_id}>
          {ticket.ticket_id} · {t(`tnTickets.origin.${ticketOrigin(ticket)}`)}
        </div>
      </td>
      <td className="tn-tix-worker" data-label={t('tnTickets.columns.worker')}>
        <span className="tn-tix-provider">{provider}</span>
        {ticket.worker_model ? (
          <div className="tn-tix-model sa-mono-id" title={ticket.worker_model}>
            {ticket.worker_model}
          </div>
        ) : null}
      </td>
      <td className="tn-tix-owner" data-label={t('tnTickets.columns.owner')} title={owner}>
        {owner}
      </td>
      <td className="tn-tix-updated sa-mono-id" data-label={t('tnTickets.columns.updated')}>
        <Age iso={ticket.updated_at} />
      </td>
    </tr>
  );
}

export default function TasksPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const searchRef = useRef<HTMLInputElement>(null);
  const bodyRef = useRef<HTMLTableSectionElement>(null);
  const attention = params.get('attention') === '1';
  const source = params.get('source') ?? '';
  const schedule = params.get('schedule') ?? '';
  const failedOnly = params.get('failed') === '1';
  // `status` is the legacy raw-status link (?status=waiting); `state` is one of the seven.
  const legacyStatus = params.get('status') ?? '';
  const stateParam = params.get('state') ?? '';
  const state: TicketStateId | '' = isTicketState(stateParam) ? stateParam : '';
  const worker = params.get('worker') ?? '';
  // The query lives in the URL (?q=), so a link or Back restores it.
  const query = params.get('q') ?? '';
  const [sort, setSort] = useState<SortId>('updated');
  const [selectedId, setSelectedId] = useState<string>('');
  const backendStatus = (state && STATE_BACKEND_STATUS[state]) || legacyStatus;
  const tickets = useInfiniteTickets({
    ...(backendStatus ? { status: backendStatus } : {}),
    ...(source ? { source_id: source } : {}),
    ...(schedule ? { schedule_id: schedule } : {}),
    ...(failedOnly ? { failed: true } : {}),
    ...(attention ? { attention: true } : {}),
    ...(query.trim() ? { q: query.trim() } : {}),
    sort,
  });
  const updateParams = (next: { state?: string; q?: string; worker?: string; attention?: boolean }) => {
    const nextState = next.state ?? state;
    const nextQuery = next.q ?? query;
    const nextWorker = next.worker ?? worker;
    const nextAttention = next.attention ?? attention;
    setParams({
      ...(source ? { source } : {}),
      ...(schedule ? { schedule } : {}),
      ...(failedOnly ? { failed: '1' } : {}),
      ...(nextAttention ? { attention: '1' } : {}),
      ...(legacyStatus && next.state === undefined ? { status: legacyStatus } : {}),
      ...(nextState ? { state: nextState } : {}),
      ...(nextWorker ? { worker: nextWorker } : {}),
      ...(nextQuery ? { q: nextQuery } : {}),
    });
  };

  const allTickets = useMemo(
    () => tickets.data?.pages.flatMap((page) => page.tickets) ?? [],
    [tickets.data],
  );
  const workers = useMemo(() => {
    const seen = new Set<string>();
    for (const ticket of allTickets) if (ticket.worker_provider) seen.add(ticket.worker_provider);
    if (worker) seen.add(worker);
    return [...seen].sort();
  }, [allTickets, worker]);
  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return allTickets
      .filter(
        (ticket) =>
          (!legacyStatus || state || ticket.status === legacyStatus) &&
          (!state || ticketState(ticket) === state) &&
          (!worker || ticket.worker_provider === worker) &&
          (!failedOnly || ['failed', 'blocked'].includes(ticket.status)) &&
          (!attention || ticket.attention) &&
          (!source || String(ticket.mandate.source_id ?? '') === source) &&
          (!schedule || String(ticket.mandate.schedule_id ?? '') === schedule) &&
          (!needle ||
            `${ticket.title} ${ticket.scope} ${ticket.origin_thread_key} ${ticket.summary} ${ticket.error}`
              .toLowerCase()
              .includes(needle)),
      )
      .sort((a, b) => {
        if (sort === 'title') return (a.title || a.origin_thread_key).localeCompare(b.title || b.origin_thread_key);
        const field = sort === 'created' ? 'created_at' : 'updated_at';
        return String(b[field]).localeCompare(String(a[field]));
      });
  }, [allTickets, attention, failedOnly, legacyStatus, query, schedule, sort, source, state, worker]);

  // The selected row is the list's single tab stop; it falls back to the first row.
  const selectedIndex = Math.max(0, rows.findIndex((row) => row.ticket_id === selectedId));
  const selected = rows[selectedIndex]?.ticket_id ?? '';

  // `/` outside a text field focuses the filter's search box.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return;
      if (isTyping(e.target)) return;
      e.preventDefault();
      searchRef.current?.focus();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  const open = (id: string) => navigate(`/tasks/${id}`);

  /** j/k (and ↓/↑) move between rows only while a row of the list has focus. */
  const onListKeyDown = (e: ReactKeyboardEvent<HTMLTableSectionElement>) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const row = (e.target as HTMLElement).closest('tr[data-ticket-id]');
    if (!row || !bodyRef.current?.contains(row)) return;
    const at = rows.findIndex((r) => r.ticket_id === row.getAttribute('data-ticket-id'));
    if (at < 0) return;
    let next: number;
    if (e.key === 'j' || e.key === 'ArrowDown') next = Math.min(rows.length - 1, at + 1);
    else if (e.key === 'k' || e.key === 'ArrowUp') next = Math.max(0, at - 1);
    else if (e.key === 'Enter') {
      if (e.nativeEvent.isComposing) return;
      e.preventDefault();
      open(rows[at].ticket_id);
      return;
    } else return;
    e.preventDefault();
    const id = rows[next].ticket_id;
    setSelectedId(id);
    bodyRef.current
      ?.querySelector<HTMLElement>(`tr[data-ticket-id="${CSS.escape(id)}"]`)
      ?.focus();
  };

  const scoped = failedOnly ? 'failed' : source ? 'source' : schedule ? 'schedule' : '';

  return (
    <div className="sa-page tn-tickets">
      <header className="sa-page-head">
        <div>
          <h1>{t('tnTickets.title')}</h1>
          <p>{t('tnTickets.lede')}</p>
        </div>
      </header>

      <section className="sa-card tn-tix-filters" aria-label={t('tnTickets.filters.label')}>
        <div className="tn-tix-fields">
          <label className="tn-tix-field tn-tix-field--grow">
            <span>{t('tnTickets.filters.search')}</span>
            <input
              ref={searchRef}
              type="search"
              value={query}
              onChange={(e) => updateParams({ q: e.target.value })}
              placeholder={t('tnTickets.filters.searchPlaceholder')}
              className="sa-search tn-tix-input"
              data-testid="tickets-search"
            />
          </label>
          <label className="tn-tix-field">
            <span>{t('tnTickets.filters.worker')}</span>
            <select
              value={worker}
              onChange={(e) => updateParams({ worker: e.target.value })}
              className="sa-filter tn-tix-input"
              data-testid="tickets-worker"
            >
              <option value="">{t('tnTickets.filters.workerAny')}</option>
              {workers.map((id) => (
                <option key={id} value={id}>
                  {nativeRuntimeLabel(id)}
                </option>
              ))}
            </select>
          </label>
          <label className="tn-tix-field">
            <span>{t('tnTickets.filters.sort')}</span>
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value as SortId)}
              className="sa-filter tn-tix-input"
              data-testid="tickets-sort"
            >
              {SORTS.map((id) => (
                <option key={id} value={id}>
                  {t(SORT_LABEL[id])}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="tn-tix-chips" role="group" aria-label={t('tnTickets.filters.states')}>
          <button
            type="button"
            className="tn-tix-chip"
            aria-pressed={!state}
            onClick={() => updateParams({ state: '' })}
          >
            {t('tnTickets.filters.all')}
          </button>
          {TICKET_STATES.map((id) => (
            <button
              key={id}
              type="button"
              className="tn-tix-chip"
              data-testid={`tickets-state-${id}`}
              aria-pressed={state === id}
              onClick={() => updateParams({ state: state === id ? '' : id })}
            >
              <StateMark state={id} />
            </button>
          ))}
          <button
            type="button"
            className="tn-tix-chip"
            aria-pressed={attention}
            onClick={() => updateParams({ attention: !attention })}
          >
            {t('tnTickets.filters.attention')}
          </button>
        </div>

        <div className="tn-tix-summary" data-testid="tickets-summary">
          <span>
            {t('tnTickets.summary.showing')}{' '}
            <b>{t('tnTickets.summary.count', { shown: rows.length, total: allTickets.length })}</b>
          </span>
          {query.trim() ? (
            <span>
              {t('tnTickets.summary.query')} <b>&quot;{query.trim()}&quot;</b>
            </span>
          ) : null}
          {worker ? (
            <span>
              {t('tnTickets.summary.worker')} <b>{nativeRuntimeLabel(worker)}</b>
            </span>
          ) : null}
          <span>{t('tnTickets.summary.keys')}</span>
        </div>
      </section>

      {tickets.isError ? <p className="sa-error">{t('tnTickets.error')}</p> : null}
      {scoped ? (
        <p className="sa-muted">
          {t(`tnTickets.scope.${scoped}`)}{' '}
          <button
            type="button"
            className="sa-link"
            onClick={() =>
              setParams({
                ...(state ? { state } : {}),
                ...(worker ? { worker } : {}),
                ...(query ? { q: query } : {}),
              })
            }
          >
            {t('tnTickets.scope.clear')}
          </button>
        </p>
      ) : null}

      {rows.length ? (
        <div className="sa-card tn-tix-card">
          <table className="tn-tix" role="grid" aria-label={t('tnTickets.list')}>
            <colgroup>
              <col className="tn-tix-col-state" />
              <col />
              <col className="tn-tix-col-worker" />
              <col className="tn-tix-col-owner" />
              <col className="tn-tix-col-updated" />
            </colgroup>
            <thead>
              <tr>
                <th scope="col">{t('tnTickets.columns.state')}</th>
                <th scope="col">{t('tnTickets.columns.ticket')}</th>
                <th scope="col">{t('tnTickets.columns.worker')}</th>
                <th scope="col">{t('tnTickets.columns.owner')}</th>
                <th scope="col">{t('tnTickets.columns.updated')}</th>
              </tr>
            </thead>
            <tbody ref={bodyRef} onKeyDown={onListKeyDown} data-testid="tickets-list">
              {rows.map((ticket) => (
                <TicketTableRow
                  key={ticket.ticket_id}
                  ticket={ticket}
                  selected={ticket.ticket_id === selected}
                  onOpen={() => open(ticket.ticket_id)}
                  onSelect={() => setSelectedId(ticket.ticket_id)}
                />
              ))}
            </tbody>
          </table>
        </div>
      ) : tickets.isSuccess ? (
        <p className="sa-muted" data-testid="tickets-empty">
          {t(allTickets.length ? 'tnTickets.emptyFiltered' : 'tnTickets.empty')}
        </p>
      ) : null}

      {tickets.hasNextPage ? (
        <button
          type="button"
          className="sa-link"
          disabled={tickets.isFetchingNextPage}
          onClick={() => void tickets.fetchNextPage()}
        >
          {tickets.isFetchingNextPage ? t('tnTickets.loading') : t('tnTickets.loadMore')}
        </button>
      ) : null}
    </div>
  );
}
