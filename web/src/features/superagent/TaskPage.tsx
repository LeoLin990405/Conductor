import { useEffect, useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate, useParams } from 'react-router';
import {
  closeAutonomyTicket,
  discardAutonomyTicket,
  getAutonomyTicketRaw,
  getTaskActivity,
  getTicketProgress,
  listAutonomySchedules,
  replyAutonomyTicket,
  retryAutonomyTicket,
  type TaskActivityEvent,
  type TicketDetail,
  type TicketProgress,
} from '@/api/autonomy';
import { useNow } from '@/hooks/useNow';
import { nativeRuntimeLabel } from '@/lib/nativeRuntimeLabels';
import { queryKeys } from '@/lib/queryKeys';
import { ClarificationPanel, TerminalTextarea, TICKET_COMPOSER_ID } from './ClarificationPanel';
import {
  StateMark,
  ticketSourceId,
  ticketTriggerId,
  useSources,
  useTicket,
  type TicketStateId,
} from './shared';
import './ticket.css';

const POLL_MS = 5_000;
const PROGRESS_TIMEOUT_MS = 15_000;

const OPEN_PHASES = new Set(['queued', 'running', 'waiting']);
const SUMMARY_PHASES = new Set(['completed', 'failed', 'cancelled']);

/**
 * Fields `TicketView` already returns that the shared `TicketDetail` client
 * type does not declare yet (worktree pin, model switches, report body). Read
 * defensively; the integrator can move them onto `TicketDetail`.
 */
type TicketDeliveryFields = {
  worktree_path?: string;
  worktree_branch?: string;
  worktree_base_sha?: string;
  worktree_host?: string;
  worktree_cleanup_pending?: boolean;
  switches?: Array<Record<string, unknown>>;
};
type TicketReport = TicketDetail['reports'][number] & { body?: string | null };
type TicketPageData = TicketDetail & TicketDeliveryFields & { reports: TicketReport[] };

function withTimeout<T>(work: Promise<T>, ms: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('progress timeout')), ms);
    work.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error: unknown) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

function isOpenProgress(data: TicketProgress | null | undefined): data is TicketProgress {
  return data != null && OPEN_PHASES.has(data.phase);
}

/** Progress phase → the terminal-native state glyph and ink. */
const PHASE_STATE: Record<string, TicketStateId> = {
  queued: 'queued',
  running: 'running',
  waiting: 'needs-you',
  completed: 'completed',
  failed: 'failed',
  cancelled: 'archived',
};
const STATE_GLYPH: Record<TicketStateId, string> = {
  queued: '○',
  running: '●',
  blocked: '▣',
  'needs-you': '◆',
  failed: '✖',
  completed: '✔',
  archived: '⊘',
};
const STEP_GLYPH: Record<string, string> = { done: '✔', active: '●', failed: '✖' };

function progressClass(phase: string): string {
  if (phase === 'waiting') return 'sa-progress tn-ticket-card sa-progress--waiting';
  if (phase === 'failed') return 'sa-progress tn-ticket-card sa-progress--failed';
  return 'sa-progress tn-ticket-card';
}

/** Map a Ticket onto one of the seven terminal-native states (the Inbox rule). */
function ticketState(ticket: { status: string; attention?: { question?: string } | null }): TicketStateId {
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

/** Whole seconds past a minute. Tenths stay visible only while the span is under a minute. */
function formatElapsed(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '0s';
  const rounded = Math.round(seconds * 10) / 10;
  const whole = Math.floor(rounded);
  const tenths = Math.round((rounded - whole) * 10);
  if (whole < 60) return tenths ? `${whole}.${tenths}s` : `${whole}s`;
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) {
    const rem = whole % 60;
    return rem ? `${minutes}m ${rem}s` : `${minutes}m`;
  }
  const hours = Math.floor(minutes / 60);
  const remMin = minutes % 60;
  return remMin ? `${hours}h ${remMin}m` : `${hours}h`;
}

function fmtDuration(ms: number): string {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function useTicketProgress(ticketId: string) {
  const progress = useQuery({
    queryKey: [...queryKeys.autonomy.ticket(ticketId), 'progress'],
    queryFn: () => withTimeout(getTicketProgress(ticketId), PROGRESS_TIMEOUT_MS),
    refetchInterval: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });

  const { data, isError, refetch } = progress;
  // A first read that times out leaves no cached card. Keep asking on the same
  // interval. A 404 is a successful empty result and stays quiet.
  const shouldPoll = isOpenProgress(data) || (data === undefined && isError);
  useEffect(() => {
    if (!shouldPoll) return;

    let timer: ReturnType<typeof setInterval> | undefined;
    const clear = () => {
      if (timer === undefined) return;
      clearInterval(timer);
      timer = undefined;
    };
    const hidden = () => document.visibilityState !== 'visible';
    const poll = () => {
      if (hidden()) {
        clear();
        return;
      }
      // Default cancelRefetch aborts the request already in flight. A payload
      // slower than the interval would then never land, so wait for it.
      void refetch({ cancelRefetch: false })
        .then((result) => {
          if (result.isError) return;
          if (!isOpenProgress(result.data)) clear();
        })
        .catch(() => undefined);
    };
    const arm = () => {
      clear();
      if (hidden()) return;
      timer = setInterval(poll, POLL_MS);
    };
    const onVisibility = () => {
      if (hidden()) {
        clear();
        return;
      }
      // A hidden tab may have been away longer than one interval. Ask now,
      // then keep the five-second cadence.
      poll();
      arm();
    };

    arm();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      clear();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [shouldPoll, refetch]);

  return progress;
}

/**
 * Worker progress: the phase as a state glyph + word, one glyph per step
 * (✔ done, ● active, ✖ failed), elapsed time, and the closing summary/body.
 * With `showEmpty` the section stays on the page with a placeholder when the
 * supervisor has no progress for this Ticket.
 */
export function TicketProgressCard({ ticketId, showEmpty = false }: { ticketId: string; showEmpty?: boolean }) {
  const { t } = useTranslation();
  const headingId = useId();
  const progress = useTicketProgress(ticketId);
  const data = progress.data;
  if (!data && !showEmpty) return null;

  const heading = (
    <h2 id={headingId} className="tn-ticket-h2">
      {t('tnTicket.section.progress')}
    </h2>
  );
  if (!data) {
    return (
      <section className="tn-ticket-section" aria-labelledby={headingId}>
        {heading}
        <div className="tn-ticket-card">
          <p className="sa-muted">{t('tnTicket.progress.none')}</p>
        </div>
      </section>
    );
  }

  const state = PHASE_STATE[data.phase] ?? 'queued';
  const phaseLabel = t(`tnTicket.progress.phase.${data.phase}`, { defaultValue: data.phase });
  const done = data.steps.filter((step) => step.state === 'done').length;

  return (
    <section className="tn-ticket-section" aria-labelledby={headingId}>
      {heading}
      <div className={progressClass(data.phase)} data-phase={data.phase}>
        <header className="sa-progress-head">
          <span className={`sa-state sa-state--${state}`}>
            <span
              className={`sa-state-glyph${state === 'running' ? ' sa-state-glyph--spin' : ''}`}
              aria-hidden="true"
            >
              {STATE_GLYPH[state]}
            </span>
            <span className="sa-state-word">{phaseLabel}</span>
          </span>
          <span className="sa-progress-elapsed">
            <span className="sa-muted">{t('tnTicket.progress.elapsed')}</span>
            <strong>{formatElapsed(data.elapsed_seconds)}</strong>
          </span>
        </header>
        <p className="sa-mono-id tn-ticket-progress-count">
          {t('tnTicket.progress.steps', { done, total: data.steps.length })}
        </p>
        <ol className="tn-ticket-steps">
          {data.steps.map((step) => (
            <li
              key={step.key}
              className={`sa-progress-step tn-ticket-step tn-ticket-step--${step.state}`}
              data-state={step.state}
              aria-current={step.state === 'active' ? 'step' : undefined}
            >
              <span className="tn-ticket-step-mark" aria-hidden="true">
                {STEP_GLYPH[step.state] ?? '○'}
              </span>
              <span className="sa-progress-step-label">{step.label}</span>
            </li>
          ))}
        </ol>
        {SUMMARY_PHASES.has(data.phase) && data.summary ? (
          <p className="sa-progress-summary tn-ticket-prose">{data.summary}</p>
        ) : null}
        {SUMMARY_PHASES.has(data.phase) &&
        data.body &&
        data.body.trim() !== (data.summary ?? '').trim() ? (
          <pre className="sa-progress-body tn-ticket-pre">{data.body}</pre>
        ) : null}
        {data.phase === 'waiting' && data.question ? (
          <p className="sa-progress-question tn-ticket-question">{data.question}</p>
        ) : null}
        {data.phase === 'failed' && data.error ? (
          <div className="sa-error sa-progress-error">{data.error}</div>
        ) : null}
      </div>
    </section>
  );
}

const ACTIVITY_KINDS = new Set(['created', 'instruction', 'source', 'report', 'closed']);

function Turn({ event }: { event: TaskActivityEvent }) {
  const { t } = useTranslation();
  const who =
    event.kind === 'report' && event.state
      ? event.state
      : ACTIVITY_KINDS.has(event.kind)
        ? t(`tnTicket.conversation.who.${event.kind}`)
        : event.kind;
  // An instruction is the operator; a report is the worker; the rest is the router.
  const side = event.kind === 'instruction' ? 'user' : event.kind === 'report' ? 'worker' : 'router';
  return (
    <article className={`tn-ticket-turn tn-ticket-turn--${side}`} data-kind={event.kind}>
      <div className="tn-ticket-who">
        {who}
        <time className="sa-mono-id" dateTime={event.at}>
          {new Date(event.at).toLocaleString()}
        </time>
      </div>
      <div className="tn-ticket-turn-body">
        {event.summary ? <p className="tn-ticket-prose">{event.summary}</p> : null}
        {event.detail ? <p className="tn-ticket-prose tn-ticket-muted">{event.detail}</p> : null}
      </div>
    </article>
  );
}

/** j/k on the focused transcript scroll one row; PageUp/PageDown stay the browser's. */
function transcriptKeys(e: ReactKeyboardEvent<HTMLDivElement>) {
  if (e.target !== e.currentTarget || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key !== 'j' && e.key !== 'k') return;
  e.preventDefault();
  const scroller = e.currentTarget.closest('main') ?? document.scrollingElement;
  scroller?.scrollBy?.(0, e.key === 'j' ? 20 : -20);
}

function switchLine(record: Record<string, unknown>): string {
  const model = (side: unknown) => {
    if (!side || typeof side !== 'object') return '';
    const s = side as { provider?: unknown; model?: unknown };
    return String(s.model ?? s.provider ?? '');
  };
  const from = model(record.from);
  const to = model(record.to);
  const reason = typeof record.reason === 'string' ? record.reason : '';
  const line = from && to ? `${from} → ${to}` : to || from;
  return reason ? `${line} (${reason})` : line;
}

type DeliveryRow = { key: string; mark: 'ok' | 'hold' | 'idle' | 'bad'; value: string; mono?: boolean };
const DELIVERY_GLYPH = { ok: '✔', hold: '◆', idle: '○', bad: '✖' } as const;

function deliveryRows(
  ticket: TicketPageData,
  latest: TicketReport | undefined,
  t: (key: string) => string,
): DeliveryRow[] {
  const none = t('tnTicket.delivery.notRecorded');
  const path = (ticket.worktree_path ?? '').trim();
  const branch = (ticket.worktree_branch ?? '').trim();
  const base = (ticket.worktree_base_sha ?? '').trim();
  const host = (ticket.worktree_host ?? '').trim();
  const state = ticketState(ticket);
  const decision: DeliveryRow =
    ticket.closed_at
      ? { key: 'decision', mark: 'idle', value: t('tnTicket.delivery.decisionClosed') }
      : state === 'completed'
        ? { key: 'decision', mark: 'ok', value: t('tnTicket.delivery.decisionDone') }
        : state === 'failed'
          ? { key: 'decision', mark: 'bad', value: t('tnTicket.delivery.decisionFailed') }
          : state === 'needs-you' || state === 'blocked'
            ? { key: 'decision', mark: 'hold', value: t('tnTicket.delivery.decisionNeedsYou') }
            : { key: 'decision', mark: 'idle', value: t('tnTicket.delivery.decisionOpen') };
  return [
    { key: 'workspace', mark: path ? 'ok' : 'idle', value: path || ticket.workspace_id || none, mono: true },
    { key: 'branch', mark: branch ? 'ok' : 'idle', value: branch || none, mono: true },
    { key: 'base', mark: base ? 'ok' : 'idle', value: base ? base.slice(0, 12) : none, mono: true },
    { key: 'host', mark: host ? 'ok' : 'idle', value: host || none, mono: true },
    {
      key: 'cleanup',
      mark: ticket.worktree_cleanup_pending ? 'hold' : 'ok',
      value: t(ticket.worktree_cleanup_pending ? 'tnTicket.delivery.cleanupPending' : 'tnTicket.delivery.cleanupClear'),
    },
    {
      key: 'gitCheck',
      mark: !latest ? 'idle' : latest.git_check === 'unsupported_runtime' ? 'hold' : 'ok',
      value: !latest
        ? t('tnTicket.delivery.noReport')
        : latest.git_check === 'unsupported_runtime'
          ? t('tnTicket.delivery.unsupported')
          : t('tnTicket.delivery.checked'),
    },
    decision,
  ];
}

function TicketComposer({
  ticket,
  canInstruct,
  note,
  daemonTask,
}: {
  ticket: TicketPageData;
  canInstruct: boolean;
  note: string;
  daemonTask: boolean;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [text, setText] = useState('');
  const [status, setStatus] = useState('');
  const sending = useRef(false);
  const answer = useMutation({
    mutationFn: (value: string) => replyAutonomyTicket(ticket.ticket_id, value),
    onSuccess: () => {
      sending.current = false;
      setText('');
      setStatus(t('tnTicket.composer.sent'));
      void qc.invalidateQueries({ queryKey: queryKeys.autonomy.all });
    },
    onError: () => {
      sending.current = false;
    },
  });
  const send = () => {
    if (!canInstruct) {
      setStatus(note);
      return;
    }
    if (sending.current || answer.isPending) return;
    const value = text.trim();
    if (!value) {
      setStatus(t('tnTicket.composer.empty'));
      return;
    }
    sending.current = true;
    answer.mutate(value);
  };
  return (
    <div className="tn-ticket-composer" role="group" aria-label={t('tnTicket.composer.label')} data-composer-box>
      <span className="tn-ticket-prompt" aria-hidden="true">›</span>
      <TerminalTextarea
        id={TICKET_COMPOSER_ID}
        data-testid="ticket-composer-input"
        className="tn-ticket-input"
        rows={2}
        aria-label={t('tnTicket.composer.label')}
        aria-describedby="ticket-composer-status"
        placeholder={canInstruct ? t('tnTicket.composer.placeholder') : note}
        readOnly={!canInstruct}
        value={text}
        onValueChange={setText}
        onSend={send}
        slashOpensPalette
      />
      <button
        type="button"
        className="sa-primary"
        disabled={!canInstruct || !text.trim() || answer.isPending}
        onClick={send}
      >
        {t('tnTicket.composer.send')}
      </button>
      {answer.isError ? (
        <p className="sa-error" role="alert">
          {t(daemonTask ? 'tnTicket.composer.daemonFailed' : 'tnTicket.composer.failed')}
        </p>
      ) : null}
      <span className="sa-mono-id" id="ticket-composer-status" role="status">
        {status || (canInstruct ? t('tnTicket.composer.status') : note)}
      </span>
    </div>
  );
}

function Pair({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/** The /tasks/:ticketId body: header, conversation + composer, progress, report, delivery check. */
export function TicketDetailPage({ ticketId }: { ticketId: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const now = useNow();
  const qc = useQueryClient();
  const sources = useSources();
  const schedules = useQuery({
    queryKey: [...queryKeys.autonomy.sources(), 'schedules'],
    queryFn: listAutonomySchedules,
    staleTime: 30_000,
  });
  const detail = useTicket(ticketId);
  const [rawOpen, setRawOpen] = useState(false);
  const activity = useQuery({
    queryKey: [...queryKeys.autonomy.ticket(ticketId), 'activity'],
    queryFn: () => getTaskActivity(ticketId),
    refetchInterval: 2_500,
  });
  const raw = useQuery({
    queryKey: [...queryKeys.autonomy.ticket(ticketId), 'raw'],
    queryFn: () => getAutonomyTicketRaw(ticketId),
    enabled: rawOpen,
    refetchInterval: rawOpen ? 2_000 : false,
  });
  const invalidate = () => void qc.invalidateQueries({ queryKey: queryKeys.autonomy.all });
  const closeTask = useMutation({ mutationFn: () => closeAutonomyTicket(ticketId), onSuccess: invalidate });
  const retry = useMutation({ mutationFn: () => retryAutonomyTicket(ticketId), onSuccess: invalidate });
  const discard = useMutation({ mutationFn: () => discardAutonomyTicket(ticketId), onSuccess: invalidate });
  const ids = {
    conversation: useId(),
    report: useId(),
    delivery: useId(),
  };

  if (detail.isLoading) return <p className="sa-muted">{t('tnTicket.loading')}</p>;
  if (detail.isError) return <p className="sa-error">{t('tnTicket.loadError')}</p>;
  if (!detail.data) return <p className="sa-muted">{t('tnTicket.notFound')}</p>;
  const ticket = detail.data as TicketPageData;
  const none = t('tnTicket.meta.none');
  const sourceId = ticketSourceId(ticket);
  const triggerId = ticketTriggerId(ticket);
  const source = (sources.data?.sources ?? []).find((s) => s.source_id === sourceId);
  const repository = (source?.config.repository ?? '').trim();
  // The sources query is optional context. A known id still names the Source.
  const sourceLabel = sourceId ? repository || sourceId : '';
  const scheduleId = ticket.mandate.schedule_id ? String(ticket.mandate.schedule_id) : '';
  const scheduleLabel = scheduleId
    ? (schedules.data?.schedules ?? []).find((s) => s.schedule_id === scheduleId)?.title ||
      t('tnTicket.meta.scheduled')
    : '';
  const originLabel = scheduleId
    ? scheduleLabel
    : source?.kind === 'manual'
      ? t('tnTicket.meta.manual')
      : sourceLabel;
  const originHref = scheduleId
    ? `/tasks?schedule=${encodeURIComponent(scheduleId)}`
    : source?.kind === 'github' && sourceId
      ? `/tasks?source=${encodeURIComponent(sourceId)}`
      : null;
  const speculative = Boolean(ticket.mandate.speculative);
  const outcome = ticket.speculative_outcome;
  const outcomeEvidence = outcome?.evidence?.worker_summary?.trim() ?? '';
  const closed = Boolean(ticket.closed_at);
  // A daemon ticket has no backend thread. The reply API accepts one only
  // while it is blocked or waiting; other states return 409.
  const daemonTask = Boolean(ticket.worker_daemon_id) && !ticket.worker_thread_id;
  const daemonCanReply = ticket.status === 'blocked' || ticket.status === 'waiting';
  const awaitingClarification = ticket.status === 'awaiting_clarification';
  const canInstruct =
    !closed &&
    !['completed', 'cancelled', 'archived', 'awaiting_clarification'].includes(ticket.status) &&
    (!daemonTask || daemonCanReply);
  const composerNote = awaitingClarification
    ? t('tnTicket.composer.trapPending')
    : daemonTask && !closed && !['completed', 'cancelled', 'archived'].includes(ticket.status)
      ? t('tnTicket.composer.daemonNote')
      : t('tnTicket.composer.closedNote');
  const duration =
    ticket.started_at && (ticket.ended_at || ticket.status === 'running')
      ? Math.max(0, new Date(ticket.ended_at ?? now).getTime() - new Date(ticket.started_at).getTime())
      : null;
  const state = ticketState(ticket);
  const reports = [...(ticket.reports ?? [])].sort(
    (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
  );
  const latest = reports.at(-1);
  const switches = Array.isArray(ticket.switches) ? ticket.switches : [];
  const canRetry = !closed && ['failed', 'blocked', 'waiting'].includes(ticket.status);

  return (
    <article className="tn-ticket-detail" data-ticket-state={state}>
      <section className="tn-ticket-card tn-ticket-head" aria-labelledby={`${ids.conversation}-title`}>
        <div className="tn-ticket-row1">
          <span className="tn-ticket-id" data-testid="ticket-id">{ticket.ticket_id}</span>
          <StateMark state={state} spin={state === 'running'} />
          <button
            type="button"
            className="sa-link-button sa-btn-sm"
            onClick={() => void navigator.clipboard?.writeText(ticket.ticket_id)}
          >
            {t('tnTicket.copyId')}
          </button>
          <span className="sa-mono-id tn-ticket-origin">{ticket.origin_thread_key}</span>
        </div>
        <h2 id={`${ids.conversation}-title`} className="tn-ticket-title">
          {ticket.title || ticket.origin_thread_key}
        </h2>
        {ticket.scope ? <p className="tn-ticket-prose tn-ticket-muted tn-ticket-scope">{ticket.scope}</p> : null}
        {ticket.error ? <div className="sa-error">{ticket.error}</div> : null}
        {ticket.attention?.question ? (
          <p className="tn-ticket-question" data-testid="ticket-attention">{ticket.attention.question}</p>
        ) : null}
        {outcomeEvidence && outcomeEvidence !== ticket.summary.trim() ? (
          <p className="tn-ticket-prose">{outcomeEvidence}</p>
        ) : null}
        <dl className="tn-ticket-kv">
          <Pair label={t('tnTicket.meta.worker')}>
            <span className="sa-provider">
              {ticket.worker_provider ? nativeRuntimeLabel(ticket.worker_provider) : t('tnTicket.meta.ptcAgent')}
            </span>
            {ticket.worker_model ? <> <span className="sa-mono-id">{ticket.worker_model}</span></> : null}
          </Pair>
          <Pair label={t('tnTicket.meta.workspace')}>
            <code className="tn-ticket-code">{ticket.workspace_id}</code>
          </Pair>
          <Pair label={t('tnTicket.meta.source')}>
            {originHref ? (
              <button type="button" className="sa-link" onClick={() => navigate(originHref)}>
                {originLabel}
              </button>
            ) : (
              <span>{originLabel || none}</span>
            )}
          </Pair>
          <Pair label={t('tnTicket.meta.sourceId')}>
            {sourceId ? <code className="tn-ticket-code">{sourceId}</code> : <span>{none}</span>}
          </Pair>
          <Pair label={t('tnTicket.meta.triggerId')}>
            {triggerId ? <code className="tn-ticket-code">{triggerId}</code> : <span>{none}</span>}
          </Pair>
          <Pair label={t('tnTicket.meta.attempts')}>
            <strong>{ticket.attempts}</strong>
          </Pair>
          {switches.length ? (
            <Pair label={t('tnTicket.meta.switches')}>
              <span className="sa-mono-id">
                {switches.length} · {switchLine(switches[switches.length - 1] ?? {})}
              </span>
            </Pair>
          ) : null}
          {speculative ? (
            <Pair label={t('tnTicket.meta.mode')}>
              <span>{t('tnTicket.meta.speculative')}</span>
            </Pair>
          ) : null}
          {outcome?.outcome ? (
            <Pair label={t('tnTicket.meta.outcome')}>
              <span>
                {outcome.outcome}
                {outcome.evidence?.actor ? ` · ${outcome.evidence.actor}` : ''}
              </span>
            </Pair>
          ) : null}
          <Pair label={t('tnTicket.meta.created')}>
            <span className="sa-mono-id">{new Date(ticket.created_at).toLocaleString()}</span>
          </Pair>
          <Pair label={t('tnTicket.meta.updated')}>
            <span className="sa-mono-id">{new Date(ticket.updated_at).toLocaleString()}</span>
          </Pair>
          <Pair label={t('tnTicket.meta.duration')}>
            <span className="sa-mono-id">{duration === null ? none : fmtDuration(duration)}</span>
          </Pair>
          <Pair label={t('tnTicket.meta.ended')}>
            <span className="sa-mono-id">{ticket.ended_at ? new Date(ticket.ended_at).toLocaleString() : none}</span>
          </Pair>
        </dl>
        <div className="tn-ticket-actions">
          {closed ? (
            <span className="sa-muted">
              {t('tnTicket.actions.closed')}
              {ticket.close_reason ? ` · ${ticket.close_reason}` : ''}
            </span>
          ) : (
            <button
              type="button"
              className="sa-link-button"
              disabled={closeTask.isPending}
              onClick={() => closeTask.mutate()}
            >
              {t('tnTicket.actions.close')}
            </button>
          )}
          {canRetry ? (
            <button type="button" className="sa-link-button" onClick={() => retry.mutate()}>
              {speculative ? t('tnTicket.actions.approve') : t('tnTicket.actions.retry')}
            </button>
          ) : null}
          {speculative && ['failed', 'blocked', 'waiting'].includes(ticket.status) ? (
            <button
              type="button"
              className="sa-link-button"
              onClick={() => discard.mutate()}
              disabled={discard.isPending}
            >
              {t('tnTicket.actions.discard')}
            </button>
          ) : null}
        </div>
        {closeTask.isError ? (
          <p className="sa-error" role="alert">{t('tnTicket.actions.closeFailed')}</p>
        ) : null}
      </section>

      <div className="tn-ticket-grid">
        <section className="tn-ticket-section" aria-labelledby={ids.conversation}>
          <h2 id={ids.conversation} className="tn-ticket-h2">{t('tnTicket.section.conversation')}</h2>
          <div
            className="tn-ticket-card tn-ticket-transcript"
            tabIndex={0}
            aria-label={t('tnTicket.conversation.label')}
            onKeyDown={transcriptKeys}
            data-transcript
          >
            {activity.isLoading ? <p className="sa-muted">{t('tnTicket.conversation.loading')}</p> : null}
            {activity.isError ? <p className="sa-error">{t('tnTicket.conversation.error')}</p> : null}
            {activity.data?.events.length
              ? activity.data.events.map((event) => <Turn key={event.event_id} event={event} />)
              : activity.data
                ? <p className="sa-muted">{t('tnTicket.conversation.empty')}</p>
                : null}
            <details
              className="tn-ticket-tool"
              open={rawOpen}
              onToggle={(e) => setRawOpen(e.currentTarget.open)}
            >
              <summary>
                {rawOpen ? t('tnTicket.conversation.logHide') : t('tnTicket.conversation.log')}
              </summary>
              {rawOpen ? (
                <div className="tn-ticket-tool-body">
                  <p className="sa-muted">{t('tnTicket.conversation.logNote')}</p>
                  {raw.data?.note ? <p className="sa-muted">{raw.data.note}</p> : null}
                  <pre className="tn-ticket-pre">
                    {(raw.data?.events ?? []).slice(-80).map((f) => `${f.id}\n${f.data}\n`).join('\n')}
                  </pre>
                </div>
              ) : null}
            </details>
          </div>
          {/* The needs-you trap sits in front of the composer: it comes first in
              the tab order and Escape inside it returns here. */}
          {awaitingClarification ? (
            <ClarificationPanel ticketId={ticket.ticket_id} returnFocusId={TICKET_COMPOSER_ID} />
          ) : null}
          <TicketComposer
            key={ticket.ticket_id}
            ticket={ticket}
            canInstruct={canInstruct}
            note={composerNote}
            daemonTask={daemonTask}
          />
        </section>

        <div className="tn-ticket-side">
          <TicketProgressCard ticketId={ticket.ticket_id} showEmpty />

          <section className="tn-ticket-section" aria-labelledby={ids.report}>
            <h2 id={ids.report} className="tn-ticket-h2">{t('tnTicket.section.report')}</h2>
            <div className="tn-ticket-card tn-ticket-report" data-testid="ticket-report">
              {latest ? (
                <>
                  <h3 className="tn-ticket-h3">
                    {t('tnTicket.report.heading')} · <span className="sa-mono-id">{latest.state}</span>{' '}
                    <time className="sa-mono-id" dateTime={latest.created_at}>
                      {new Date(latest.created_at).toLocaleString()}
                    </time>
                  </h3>
                  {latest.summary ? <p className="tn-ticket-prose">{latest.summary}</p> : null}
                  {latest.body && latest.body.trim() !== latest.summary.trim() ? (
                    <pre className="tn-ticket-pre">{latest.body}</pre>
                  ) : null}
                  {latest.question ? (
                    <p className="tn-ticket-question">
                      <span className="tn-ticket-label">{t('tnTicket.report.question')}</span> {latest.question}
                    </p>
                  ) : null}
                </>
              ) : (
                <p className="sa-muted">{t('tnTicket.report.empty')}</p>
              )}
            </div>
          </section>

          <section className="tn-ticket-section" aria-labelledby={ids.delivery}>
            <h2 id={ids.delivery} className="tn-ticket-h2">{t('tnTicket.section.delivery')}</h2>
            <div className="tn-ticket-card tn-ticket-delivery" data-testid="ticket-delivery">
              <dl className="tn-ticket-check">
                {deliveryRows(ticket, latest, t).map((row) => (
                  <div key={row.key} data-check={row.key} data-mark={row.mark}>
                    <dt className={`tn-ticket-mark tn-ticket-mark--${row.mark}`} aria-hidden="true">
                      {DELIVERY_GLYPH[row.mark]}
                    </dt>
                    <dt>{t(`tnTicket.delivery.${row.key}`)}</dt>
                    <dd className={row.mono ? 'tn-ticket-code' : undefined}>{row.value}</dd>
                  </div>
                ))}
              </dl>
              <p className="tn-ticket-contract">{t('tnTicket.delivery.contract')}</p>
            </div>
          </section>
        </div>
      </div>
    </article>
  );
}

export default function TaskPage() {
  const { t } = useTranslation();
  const { ticketId } = useParams();
  const navigate = useNavigate();
  return (
    <div className="sa-page tn-ticket">
      <header className="tn-ticket-page-head">
        <button type="button" className="sa-link" onClick={() => navigate(-1)}>
          {t('tnTicket.back')}
        </button>
        <h1>{t('tnTicket.title')}</h1>
        <p className="tn-ticket-muted">{t('tnTicket.lede')}</p>
      </header>
      {ticketId ? <TicketDetailPage ticketId={ticketId} /> : <p className="sa-muted">{t('tnTicket.missing')}</p>}
    </div>
  );
}
