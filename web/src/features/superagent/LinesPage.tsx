import { useCallback, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { getAutonomyTicketLines, type AutonomyLineGroup, type AutonomyLineTicket } from '@/api/autonomy';
import { useLocale } from '@/hooks/useLocale';
import { useNow } from '@/hooks/useNow';
import { registerFeatureLocales } from '@/lib/featureLocales';
import { relativeTime } from '@/lib/format';
import { queryKeys } from '@/lib/queryKeys';
import { StateMark, type TicketStateId } from './shared';
import tnLinesEn from './locales/tnLines.en-US.json';
import tnLinesZh from './locales/tnLines.zh-CN.json';
import './lines.css';

// Lines copy lives in its own tnLines pair; registering at module load means
// every t('tnLines.…') resolves without touching the main catalogs.
registerFeatureLocales({ 'en-US': tnLinesEn, 'zh-CN': tnLinesZh });

/** Ticket status → terminal-native state id (the Inbox grammar). */
function lineTicketState(status: string): TicketStateId {
  switch (status) {
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
 * One state for a whole line, rolled up from its Tickets: anything that needs
 * a person wins, then a failure, then live work, then queued work; a line with
 * nothing open is completed (or archived when the line itself is).
 */
function lineState(group: AutonomyLineGroup): TicketStateId {
  if (group.line?.status === 'archived') return 'archived';
  const states = new Set(group.tickets.map((ticket) => lineTicketState(ticket.status)));
  for (const s of ['needs-you', 'failed', 'blocked', 'running', 'queued'] as const) {
    if (states.has(s)) return s;
  }
  if (group.ticket_count > 0 && group.open_count === 0) return 'completed';
  return group.open_count > 0 ? 'queued' : 'completed';
}

/** The worker on a line: the provider of its most recently active Ticket. */
function lineWorker(group: AutonomyLineGroup): string {
  const withWorker = group.tickets.filter((ticket) => ticket.worker_provider);
  if (withWorker.length === 0) return '';
  const stamp = (ticket: AutonomyLineTicket) => Date.parse(ticket.last_activity_at ?? ticket.updated_at) || 0;
  return [...withWorker].sort((a, b) => stamp(b) - stamp(a))[0].worker_provider;
}

const groupKey = (group: AutonomyLineGroup) => group.line?.line_id ?? 'ungrouped';

const ROW_SELECTOR = '[data-tn-row]';

export default function LinesPage() {
  const { t } = useTranslation();
  const locale = useLocale();
  const now = useNow();
  const lines = useQuery({
    queryKey: queryKeys.autonomy.lines(),
    queryFn: () => getAutonomyTicketLines(false),
    refetchInterval: 15_000,
  });
  const groups = lines.data ? [...lines.data.lines, ...(lines.data.ungrouped ? [lines.data.ungrouped] : [])] : [];

  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  const [activeRow, setActiveRow] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const toggle = useCallback((key: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  // Roving tab stop: one row in the list is tabbable; the first row until the
  // person lands on another one.
  const rowKeys: string[] = [];
  for (const group of groups) {
    const key = groupKey(group);
    rowKeys.push(`line:${key}`);
    if (!collapsed.has(key)) for (const ticket of group.tickets) rowKeys.push(`ticket:${ticket.ticket_id}`);
  }
  const tabStop = activeRow && rowKeys.includes(activeRow) ? activeRow : rowKeys[0];

  /** j/k (and ↓/↑) move between rows, but only while focus is on a list row. */
  const onListKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.ctrlKey || event.metaKey || event.altKey || event.nativeEvent.isComposing) return;
    const step =
      event.key === 'j' || event.key === 'ArrowDown' ? 1 : event.key === 'k' || event.key === 'ArrowUp' ? -1 : 0;
    if (step === 0) return;
    const target = event.target as HTMLElement;
    const row = target.closest<HTMLElement>(ROW_SELECTOR);
    if (!row || row !== target) return;
    const rows = Array.from(listRef.current?.querySelectorAll<HTMLElement>(ROW_SELECTOR) ?? []);
    const index = rows.indexOf(row);
    if (index < 0) return;
    event.preventDefault();
    const next = rows[Math.max(0, Math.min(rows.length - 1, index + step))];
    next.focus();
  };

  const rowProps = (key: string) => ({
    'data-tn-row': key,
    tabIndex: key === tabStop ? 0 : -1,
    onFocus: () => setActiveRow(key),
  });

  return (
    <div className="sa-page tn-lines h-full">
      <header className="sa-page-head">
        <div>
          <h1>{t('tnLines.title')}</h1>
          <p>{t('tnLines.lede')}</p>
        </div>
        <div className="tn-lines-actions">
          <span className="tn-lines-hint" aria-hidden="true">{t('tnLines.hint')}</span>
          <button
            type="button"
            className="tn-lines-button"
            onClick={() => void lines.refetch()}
            disabled={lines.isFetching}
          >
            {t('tnLines.refresh')}
          </button>
        </div>
      </header>
      {lines.isPending ? <p className="tn-lines-muted" role="status">{t('tnLines.loading')}</p> : null}
      {lines.isError ? <p className="tn-lines-error" role="alert">{t('tnLines.error')}</p> : null}
      {lines.isSuccess && groups.length === 0 ? <p className="tn-lines-muted">{t('tnLines.empty')}</p> : null}
      {groups.length > 0 ? (
        <div
          ref={listRef}
          className="tn-lines-list"
          role="group"
          aria-label={t('tnLines.listLabel')}
          onKeyDown={onListKeyDown}
        >
          <div className="tn-lines-row tn-lines-row--line tn-lines-colhead" aria-hidden="true">
            <span />
            <span>{t('tnLines.col.state')}</span>
            <span>{t('tnLines.col.line')}</span>
            <span>{t('tnLines.col.worker')}</span>
            <span>{t('tnLines.col.branch')}</span>
            <span className="tn-lines-counts">{t('tnLines.col.tickets')}</span>
          </div>
          {groups.map((group) => {
            const key = groupKey(group);
            return (
              <LineGroup
                key={key}
                group={group}
                expanded={!collapsed.has(key)}
                onToggle={() => toggle(key)}
                rowProps={rowProps}
                locale={locale}
                now={now}
              />
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

type RowProps = (key: string) => {
  'data-tn-row': string;
  tabIndex: number;
  onFocus: () => void;
};

function LineGroup({ group, expanded, onToggle, rowProps, locale, now }: {
  group: AutonomyLineGroup;
  expanded: boolean;
  onToggle: () => void;
  rowProps: RowProps;
  locale: string;
  now: number;
}) {
  const { t } = useTranslation();
  const line = group.line;
  const key = groupKey(group);
  const name = line ? line.name : t('tnLines.ungrouped');
  const worker = lineWorker(group);
  const state = lineState(group);
  const idBase = `tn-line-${key}`;
  const bodyId = `${idBase}-tickets`;
  return (
    <section className="tn-lines-group" aria-labelledby={`${idBase}-title`}>
      <h2 className="tn-lines-heading">
        <button
          type="button"
          className="tn-lines-row tn-lines-row--line"
          aria-expanded={expanded}
          aria-controls={bodyId}
          aria-labelledby={`${idBase}-title`}
          aria-describedby={`${idBase}-state ${idBase}-meta`}
          title={t('tnLines.toggle', { name })}
          onClick={onToggle}
          {...rowProps(`line:${key}`)}
        >
          <span className="tn-lines-caret" aria-hidden="true">{expanded ? '▾' : '▸'}</span>
          <span id={`${idBase}-state`} className="tn-lines-state"><StateMark state={state} spin={state === 'running'} /></span>
          <span id={`${idBase}-title`} className="tn-lines-name tn-lines-trunc">{name}</span>
          <span id={`${idBase}-meta`} className="tn-lines-meta">
            <span className="tn-lines-worker tn-lines-trunc">{worker || t('tnLines.noWorker')}</span>
            <code className="tn-lines-branch tn-lines-trunc">
              {line ? line.branch || t('tnLines.noBranch') : '—'}
            </code>
            <span className="tn-lines-counts">
              {t('tnLines.counts', { open: group.open_count, total: group.ticket_count })}
            </span>
          </span>
        </button>
      </h2>
      {expanded ? (
        <div id={bodyId} className="tn-lines-body">
          {line ? (
            <code className="tn-lines-worktree tn-lines-trunc" title={line.worktree_path}>{line.worktree_path}</code>
          ) : null}
          {group.tickets.length === 0 ? <p className="tn-lines-muted tn-lines-none">{t('tnLines.noTickets')}</p> : null}
          {group.tickets.map((ticket) => (
            <LineTicketRow key={ticket.ticket_id} ticket={ticket} rowProps={rowProps} locale={locale} now={now} />
          ))}
        </div>
      ) : null}
    </section>
  );
}

function LineTicketRow({ ticket, rowProps, locale, now }: {
  ticket: AutonomyLineTicket;
  rowProps: RowProps;
  locale: string;
  now: number;
}) {
  const activity = relativeTime(ticket.last_activity_at, locale, now);
  const state = lineTicketState(ticket.status);
  const who = [ticket.owner, ticket.worker_provider].filter(Boolean).join(' · ');
  return (
    <Link
      to={`/tasks/${ticket.ticket_id}`}
      className="tn-lines-row tn-lines-row--ticket"
      {...rowProps(`ticket:${ticket.ticket_id}`)}
    >
      <span aria-hidden="true" />
      <span className="tn-lines-state"><StateMark state={state} spin={state === 'running'} /></span>
      <span className="tn-lines-title tn-lines-trunc" title={ticket.title}>{ticket.title}</span>
      <span className="tn-lines-who tn-lines-trunc">{who}</span>
      <span className="tn-lines-age">{activity}</span>
    </Link>
  );
}
