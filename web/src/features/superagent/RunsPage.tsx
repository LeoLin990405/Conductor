import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { useSearchParams } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { getAutonomyTicket, getAutonomyTicketRaw, type AutonomyTicket } from '@/api/autonomy';
import { useLocale } from '@/hooks/useLocale';
import { useNow } from '@/hooks/useNow';
import { registerFeatureLocales } from '@/lib/featureLocales';
import { relativeTime } from '@/lib/format';
import { nativeRuntimeLabel } from '@/lib/nativeRuntimeLabels';
import { queryKeys } from '@/lib/queryKeys';
import { StateMark, useInfiniteTickets, useSources, type TicketStateId } from './shared';
import tnRunsEn from './locales/tnRuns.en-US.json';
import tnRunsZh from './locales/tnRuns.zh-CN.json';
import './runs.css';

// Runs copy lives in its own tnRuns pair; registering at module load means
// every t('tnRuns.…') resolves without touching the main catalogs.
registerFeatureLocales({ 'en-US': tnRunsEn, 'zh-CN': tnRunsZh });

/** Map a run's Ticket onto one of the seven terminal-native states. */
function runState(ticket: Pick<AutonomyTicket, 'status' | 'attention'>): TicketStateId {
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
 * Whole seconds a run has taken: started → ended, or started → now while it is
 * still running. `null` when the run never started (or the stamps are bad).
 */
function runSeconds(
  ticket: Pick<AutonomyTicket, 'started_at' | 'ended_at' | 'status'>,
  now: number,
): number | null {
  if (!ticket.started_at) return null;
  const start = Date.parse(ticket.started_at);
  if (Number.isNaN(start)) return null;
  const endStamp = ticket.ended_at ? Date.parse(ticket.ended_at) : ticket.status === 'running' ? now : NaN;
  if (Number.isNaN(endStamp)) return null;
  return Math.max(0, Math.floor((endStamp - start) / 1000));
}

const pad2 = (n: number) => String(n).padStart(2, '0');

function Duration({ seconds }: { seconds: number | null }) {
  const { t } = useTranslation();
  if (seconds === null) return <>{t('tnRuns.duration.none')}</>;
  if (seconds < 60) return <>{t('tnRuns.duration.s', { s: seconds })}</>;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return <>{t('tnRuns.duration.ms', { m: minutes, s: pad2(seconds % 60) })}</>;
  return <>{t('tnRuns.duration.hm', { h: Math.floor(minutes / 60), m: pad2(minutes % 60) })}</>;
}

const ROW_SELECTOR = '[data-tn-run]';

export default function RunsPage() {
  const { t } = useTranslation();
  const locale = useLocale();
  const navigate = useNavigate();
  const tickets = useInfiniteTickets();
  const [search, setSearch] = useSearchParams();
  const requested = search.get('task');
  const rows = (tickets.data?.pages.flatMap((page) => page.tickets) ?? []).filter((t) => t.worker_thread_id);
  const anyRunning = rows.some((row) => row.status === 'running');
  // Live durations tick every second while a run is going; ages every minute.
  const now = useNow(anyRunning ? 1_000 : 60_000);
  const listRef = useRef<HTMLDivElement>(null);
  const [focusedRow, setFocusedRow] = useState<string | null>(null);
  // The selection lives in the URL: ?task= when present, else the first run,
  // which is then written back so the address names what is shown.
  const firstRun = rows[0]?.ticket_id ?? null;
  const selected = requested ?? firstRun;
  useEffect(() => {
    if (!requested && firstRun) setSearch({ task: firstRun });
  }, [requested, firstRun, setSearch]);
  const listed = rows.find((t) => t.ticket_id === selected);
  const detail = useQuery({
    queryKey: queryKeys.autonomy.ticket(selected ?? ''),
    queryFn: () => getAutonomyTicket(selected!),
    enabled: Boolean(selected) && !listed,
  });
  const selectedTitle = listed?.title || detail.data?.title || t('tnRuns.fallbackTitle');
  const selectedWorker = listed
    ? { provider: listed.worker_provider, model: listed.worker_model }
    : { provider: detail.data?.worker_provider, model: detail.data?.worker_model };
  const raw = useQuery({
    queryKey: ['autonomy', 'run', selected],
    queryFn: () => getAutonomyTicketRaw(selected!),
    enabled: Boolean(selected),
    refetchInterval: 2_000,
  });
  const events = raw.data?.events ?? [];

  const open = (ticketId: string) => {
    setSearch({ task: ticketId });
  };

  // Roving tab stop: one run row is tabbable — the one last focused, else the
  // selected run, else the first.
  const ids = rows.map((row) => row.ticket_id);
  const tabStop =
    focusedRow && ids.includes(focusedRow) ? focusedRow : selected && ids.includes(selected) ? selected : ids[0];

  /** j/k (and ↓/↑) move between run rows while focus is on a row. */
  const onListKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.ctrlKey || event.metaKey || event.altKey || event.nativeEvent.isComposing) return;
    const step =
      event.key === 'j' || event.key === 'ArrowDown' ? 1 : event.key === 'k' || event.key === 'ArrowUp' ? -1 : 0;
    if (step === 0) return;
    const target = event.target as HTMLElement;
    const row = target.closest<HTMLElement>(ROW_SELECTOR);
    if (!row || row !== target) return;
    const all = Array.from(listRef.current?.querySelectorAll<HTMLElement>(ROW_SELECTOR) ?? []);
    const index = all.indexOf(row);
    if (index < 0) return;
    event.preventDefault();
    all[Math.max(0, Math.min(all.length - 1, index + step))].focus();
  };

  return (
    <div className="sa-page tn-runs">
      <header className="sa-page-head">
        <div>
          <h1>{t('tnRuns.title')}</h1>
          <p>{t('tnRuns.lede')}</p>
        </div>
        {rows.length ? <span className="tn-runs-hint" aria-hidden="true">{t('tnRuns.hint')}</span> : null}
      </header>
      {tickets.isError || raw.isError ? <p className="tn-runs-error" role="alert">{t('tnRuns.error')}</p> : null}
      <div className="tn-runs-split">
        <div className="tn-runs-listwrap">
          {rows.length ? (
            <div
              ref={listRef}
              className="tn-runs-list"
              role="group"
              aria-label={t('tnRuns.listLabel')}
              onKeyDown={onListKeyDown}
            >
              <div className="tn-runs-row tn-runs-colhead" aria-hidden="true">
                <span>{t('tnRuns.col.state')}</span>
                <span>{t('tnRuns.col.run')}</span>
                <span>{t('tnRuns.col.worker')}</span>
                <span>{t('tnRuns.col.origin')}</span>
                <span className="tn-runs-num">{t('tnRuns.col.duration')}</span>
                <span className="tn-runs-num">{t('tnRuns.col.updated')}</span>
              </div>
              {rows.map((ticket) => (
                <RunRow
                  key={ticket.ticket_id}
                  ticket={ticket}
                  active={selected === ticket.ticket_id}
                  tabbable={tabStop === ticket.ticket_id}
                  now={now}
                  locale={locale}
                  onFocus={() => setFocusedRow(ticket.ticket_id)}
                  onOpen={() => open(ticket.ticket_id)}
                />
              ))}
            </div>
          ) : tickets.isPending ? (
            <p className="tn-runs-muted" role="status">{t('tnRuns.loading')}</p>
          ) : (
            <p className="tn-runs-muted">{t('tnRuns.empty')}</p>
          )}
        </div>
        <section className="tn-runs-detail" aria-label={selected ? selectedTitle : undefined}>
          {!selected ? <p className="tn-runs-muted">{t('tnRuns.selectPrompt')}</p> : null}
          {selected ? (
            <>
              <div className="tn-runs-detail-head">
                <strong className="tn-runs-detail-title">{selectedTitle}</strong>
                <span className="tn-runs-muted tn-runs-detail-worker">
                  {selectedWorker.provider || t('tnRuns.fallbackWorker')}
                  {selectedWorker.model ? ` · ${selectedWorker.model}` : ''}
                </span>
                <span className="tn-runs-detail-actions">
                  <button
                    type="button"
                    className="tn-runs-button"
                    onClick={() =>
                      void navigator.clipboard?.writeText(events.map((f) => `${f.id}\n${f.data}\n`).join('\n'))
                    }
                  >
                    {t('tnRuns.copyRaw')}
                  </button>
                  <button type="button" className="tn-runs-button" onClick={() => navigate(`/tasks/${selected}`)}>
                    {t('tnRuns.openTask')}
                  </button>
                </span>
              </div>
              {raw.data?.note ? <p className="tn-runs-muted tn-runs-note">{raw.data.note}</p> : null}
              <pre className="tn-runs-raw" aria-label={t('tnRuns.rawLabel')} tabIndex={0}>
                {events.length
                  ? events.slice(-120).map((f) => `${f.id}\n${f.data}\n`).join('\n')
                  : raw.isSuccess
                    ? t('tnRuns.rawEmpty')
                    : ''}
              </pre>
            </>
          ) : null}
        </section>
      </div>
    </div>
  );
}

function RunRow({ ticket, active, tabbable, now, locale, onFocus, onOpen }: {
  ticket: AutonomyTicket;
  active: boolean;
  tabbable: boolean;
  now: number;
  locale: string;
  onFocus: () => void;
  onOpen: () => void;
}) {
  const { t } = useTranslation();
  const sources = useSources();
  const sourceId = ticket.mandate.source_id ? String(ticket.mandate.source_id) : '';
  const repository = sourceId
    ? ((sources.data?.sources ?? []).find((s) => s.source_id === sourceId)?.config.repository ?? '')
    : '';
  const origin = ticket.mandate.schedule_id
    ? t('tnRuns.origin.schedule')
    : ticket.origin_thread_key.startsWith('manual:')
      ? t('tnRuns.origin.manual')
      : t('tnRuns.origin.github');
  const state = runState(ticket);
  const worker = ticket.worker_provider ? nativeRuntimeLabel(ticket.worker_provider) : t('tnRuns.fallbackWorker');
  const title = ticket.title || ticket.origin_thread_key;
  return (
    <button
      type="button"
      className={`tn-runs-row tn-runs-run${active ? ' tn-runs-run--active' : ''}`}
      data-tn-run={ticket.ticket_id}
      data-state={state}
      aria-current={active ? 'true' : undefined}
      tabIndex={tabbable ? 0 : -1}
      onFocus={onFocus}
      onClick={onOpen}
    >
      <span className="tn-runs-state"><StateMark state={state} spin={state === 'running'} /></span>
      <span className="tn-runs-title tn-runs-trunc" title={title}>{title}</span>
      <span className="tn-runs-worker tn-runs-trunc">
        {worker}
        {ticket.worker_model ? ` · ${ticket.worker_model}` : ''}
      </span>
      <span className="tn-runs-origin tn-runs-trunc">{origin}{repository ? ` · ${repository}` : ''}</span>
      <span className="tn-runs-num tn-runs-duration"><Duration seconds={runSeconds(ticket, now)} /></span>
      <span className="tn-runs-num tn-runs-age">{relativeTime(ticket.updated_at, locale, now)}</span>
      {ticket.attention ? (
        <span className="tn-runs-attention">{ticket.attention.question || t('tnRuns.needsPerson')}</span>
      ) : null}
    </button>
  );
}
