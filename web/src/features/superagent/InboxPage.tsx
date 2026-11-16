import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { nativeRuntimeLabel } from '@/lib/nativeRuntimeLabels';
import { queryKeys } from '@/lib/queryKeys';
import {
  confirmTicketClarification,
  getAutonomyOverview,
  getTicketClarification,
  replyAutonomyTicket,
  type AutonomyTicket,
} from '@/api/autonomy';
import {
  ReplyComposer,
  StateMark,
  TaskComposer,
  Age,
  useTickets,
  type TicketStateId,
} from './shared';

/** Map a Ticket onto one of the seven terminal-native states. */
function ticketState(ticket: {
  status: string;
  attention?: { question?: string } | null;
}): TicketStateId {
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

/** Tickets that need a person: a decision, a reply or a retry. */
const NEEDS_PERSON: TicketStateId[] = ['needs-you', 'failed', 'blocked'];

/**
 * The row cap `listAutonomyTickets` asks for (and the backend clamps to). A
 * category list that comes back this long may have been cut, so a total built
 * from it is a lower bound and is shown as `N+`.
 */
const TICKET_PAGE_CAP = 200;

/** A worker question: the answer is stored as an instruction on the Ticket. */
function ReplyTrap({ ticket }: { ticket: AutonomyTicket }) {
  return (
    <ReplyComposer
      ticket={ticket}
      copy="reply"
      submit={(text) => replyAutonomyTicket(ticket.ticket_id, text)}
    />
  );
}

/**
 * An `awaiting_clarification` Ticket is parked until its drafted request is
 * confirmed; an instruction would be stored and leave it parked. So its trap
 * is the clarification flow the Ticket page uses (same query, same confirm
 * route): the draft is prefilled and Enter confirms it — edited or as drafted.
 */
function ClarifyTrap({ ticket }: { ticket: AutonomyTicket }) {
  const { t } = useTranslation();
  const clarification = useQuery({
    queryKey: [...queryKeys.autonomy.ticket(ticket.ticket_id), 'clarification'],
    queryFn: () => getTicketClarification(ticket.ticket_id),
    retry: false,
  });
  if (clarification.isLoading) {
    return <p className="sa-muted" data-testid="inbox-clarify-loading">{t('tnInbox.clarify.loading')}</p>;
  }
  if (clarification.isError || !clarification.data) {
    return <p className="sa-muted" data-testid="inbox-clarify-unavailable">{t('tnInbox.clarify.unavailable')}</p>;
  }
  const data = clarification.data;
  return (
    <>
      {data.open_questions.length ? (
        <div className="sa-note">
          <span className="sa-muted">{t('tnInbox.clarify.questions')}</span>
          <ol className="sa-clarify-list">
            {data.open_questions.map((item) => <li key={item}>{item}</li>)}
          </ol>
        </div>
      ) : null}
      <ReplyComposer
        ticket={ticket}
        copy="clarify"
        initial={data.expanded_request}
        submit={(text) => confirmTicketClarification(ticket.ticket_id, text)}
      />
    </>
  );
}

function NeedsYouCard({ ticket }: { ticket: AutonomyTicket }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const state = ticketState(ticket);
  const question = ticket.attention?.question?.trim() ?? '';
  return (
    <article className="sa-card sa-ticket-card">
      <div className="sa-ticket-top">
        <StateMark state={state} />
        <div className="sa-ticket-body">
          <div className="sa-inbox-title">
            <button type="button" className="sa-link" onClick={() => navigate(`/tasks/${ticket.ticket_id}`)}>
              {ticket.title || ticket.origin_thread_key}
            </button>
          </div>
        </div>
      </div>
      <div className="sa-meta">
        <span className="sa-mono-id">{ticket.ticket_id}</span>
        {ticket.worker_provider ? <span className="sa-provider">{nativeRuntimeLabel(ticket.worker_provider)}</span> : null}
        {ticket.worker_model ? <span className="sa-model">{ticket.worker_model}</span> : null}
        <span className="sa-mono-id">
          {t(state === 'failed' ? 'tnInbox.meta.failed' : 'tnInbox.meta.waiting')} <Age iso={ticket.updated_at} />
        </span>
      </div>
      {question ? <p className="sa-note">{question}</p> : null}
      {ticket.error ? <p className="sa-error">{ticket.error}</p> : null}
      {ticket.status === 'awaiting_clarification' ? (
        <ClarifyTrap ticket={ticket} />
      ) : question ? (
        <ReplyTrap ticket={ticket} />
      ) : null}
    </article>
  );
}

function RunningCard({ ticket }: { ticket: AutonomyTicket }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <article className="sa-card sa-ticket-card">
      <div className="sa-ticket-top">
        <StateMark state="running" spin />
        <div className="sa-ticket-body">
          <div className="sa-inbox-title">
            <button type="button" className="sa-link" onClick={() => navigate(`/tasks/${ticket.ticket_id}`)}>
              {ticket.title || ticket.origin_thread_key}
            </button>
          </div>
        </div>
      </div>
      {ticket.summary ? <p className="sa-ticket-sub">{ticket.summary}</p> : null}
      <div className="sa-meta">
        <span className="sa-mono-id">{ticket.ticket_id}</span>
        {ticket.worker_provider ? <span className="sa-provider">{nativeRuntimeLabel(ticket.worker_provider)}</span> : null}
        {ticket.worker_model ? <span className="sa-model">{ticket.worker_model}</span> : null}
        <span className="sa-mono-id">{t('tnInbox.meta.running')} <Age iso={ticket.updated_at} /></span>
      </div>
    </article>
  );
}

export default function InboxPage() {
  const { t } = useTranslation();
  // Each active category is queried on its own so a long tail of completed
  // Tickets can never crowd a pending question off the capped 200-row page.
  // `failed: true` is the backend's failed + blocked filter. A Ticket pending
  // review sits in `waiting` with attention:null, and a new Ticket parked on its
  // clarification gate sits in `awaiting_clarification` — the attention filter
  // returns neither — so both are fetched on their own and folded into
  // needs-you (ticketState maps both onto it).
  const needsYouQuery = useTickets({ attention: true });
  const failedQuery = useTickets({ failed: true });
  const waitingQuery = useTickets({ status: 'waiting' });
  const clarifyQuery = useTickets({ status: 'awaiting_clarification' });
  const runningQuery = useTickets({ status: 'running' });
  const queuedQuery = useTickets({ status: 'queued' });
  const overview = useQuery({
    queryKey: queryKeys.autonomy.overview(),
    queryFn: getAutonomyOverview,
    refetchInterval: 2_500,
  });

  // Each list is held to its own filter, so a row a category was never meant to
  // hold cannot inflate that category's share of the total.
  const attentionRows = (needsYouQuery.data?.tickets ?? []).filter((row) => row.attention);
  const otherNeedRows = [
    ...(failedQuery.data?.tickets ?? []),
    ...(waitingQuery.data?.tickets ?? []),
    ...(clarifyQuery.data?.tickets ?? []),
  ];
  const needsYouById = new Map<string, AutonomyTicket>();
  for (const row of [...attentionRows, ...otherNeedRows]) {
    if (NEEDS_PERSON.includes(ticketState(row))) needsYouById.set(row.ticket_id, row);
  }
  const needsYou = [...needsYouById.values()];
  const runningRows = runningQuery.data?.tickets ?? [];
  const queuedRows = queuedQuery.data?.tickets ?? [];
  const running = runningRows.filter((row) => ticketState(row) === 'running');
  const queued = queuedRows.filter((row) => ticketState(row) === 'queued');

  const anyError =
    needsYouQuery.isError ||
    failedQuery.isError ||
    waitingQuery.isError ||
    clarifyQuery.isError ||
    runningQuery.isError ||
    queuedQuery.isError;
  const capped = (rows: unknown[]) => rows.length >= TICKET_PAGE_CAP;
  // Authoritative per-status totals come from the overview; the capped lists
  // only fill the cards, so a 201st running Ticket still reads as 201 rather
  // than the 200 the page happened to fetch. Until the overview lands, a capped
  // list reads as a lower bound (`200+`), never as an exact figure.
  const counts = overview.data?.counts;
  const runningTotal = counts ? `${counts.running}` : `${running.length}${capped(runningRows) ? '+' : ''}`;
  const queuedTotal = counts ? `${counts.queued}` : `${queued.length}${capped(queuedRows) ? '+' : ''}`;
  // Needs-you is a union with no single server total: a failed or waiting
  // Ticket can also carry a question, so summing the categories double-counts.
  // Every Ticket with a question is in `attention`, so the union is exactly
  // (attention total) + (distinct question-free rows of the other categories).
  // The attention total is the overview's, so a capped attention page is still
  // counted in full; the question-free rows are only known from their own
  // lists, so when one of those lists is capped the figure is a lower bound and
  // is shown as `N+` rather than as an exact count.
  const questionFree = new Set(
    otherNeedRows.filter((row) => !row.attention && NEEDS_PERSON.includes(ticketState(row))).map((row) => row.ticket_id),
  );
  const attentionTotal = counts ? Math.max(counts.attention, attentionRows.length) : attentionRows.length;
  const needsYouPartial =
    capped(failedQuery.data?.tickets ?? []) ||
    capped(waitingQuery.data?.tickets ?? []) ||
    capped(clarifyQuery.data?.tickets ?? []) ||
    (!counts && capped(attentionRows));

  // Emptiness follows the overview, not the capped page: a pending question the
  // list never surfaced must not read as "Nothing needs you".
  const overviewNeeds = counts ? counts.attention + counts.failed + (counts.blocked ?? 0) : 0;
  // Every needs-you source must have answered too: the waiting and
  // awaiting_clarification lists are not in the overview's counts, so before
  // they load "Nothing needs you" could flash over a Ticket that needs you.
  const needsSourcesLoaded = [needsYouQuery, failedQuery, waitingQuery, clarifyQuery].every((q) => q.isSuccess);
  // Until then the badge reads as unknown, not as a confident 0.
  const needsYouTotal = needsSourcesLoaded
    ? `${attentionTotal + questionFree.size}${needsYouPartial ? '+' : ''}`
    : '…';
  const needsYouEmpty = needsYou.length === 0 && needsSourcesLoaded && counts != null && overviewNeeds === 0;

  return (
    <div className="sa-page sa-inbox">
      <header className="sa-page-head">
        <div>
          <h1>{t('tnInbox.title')}</h1>
          <p>{t('tnInbox.lede')}</p>
        </div>
      </header>
      {anyError ? (
        <p className="sa-error">{t('tnInbox.loadError')}</p>
      ) : null}

      <section className="sa-section" aria-labelledby="sa-needs-you">
        <h2 id="sa-needs-you">
          {t('tnInbox.needsYou')} <span className="sa-count sa-count--hold">◆ {needsYouTotal}</span>
        </h2>
        {needsYou.length ? (
          <div className="sa-inbox-grid">
            {needsYou.map((ticket) => (
              <NeedsYouCard key={ticket.ticket_id} ticket={ticket} />
            ))}
          </div>
        ) : needsYouEmpty ? (
          <p className="sa-muted">{t('tnInbox.empty')}</p>
        ) : null}
      </section>

      {running.length ? (
        <section className="sa-section" aria-labelledby="sa-running">
          <h2 id="sa-running">
            {t('tnInbox.running')} <span className="sa-count sa-count--tempo">● {runningTotal}</span>
          </h2>
          <div className="sa-inbox-grid sa-inbox-grid--3">
            {running.map((ticket) => (
              <RunningCard key={ticket.ticket_id} ticket={ticket} />
            ))}
          </div>
        </section>
      ) : null}

      {queued.length ? (
        <section className="sa-section" aria-labelledby="sa-queued">
          <h2 id="sa-queued">
            {t('tnInbox.queued')} <span className="sa-count sa-count--muted">○ {queuedTotal}</span>
          </h2>
          <div className="sa-queue">
            {queued.map((ticket) => (
              <QueueRow key={ticket.ticket_id} ticket={ticket} />
            ))}
          </div>
        </section>
      ) : null}

      <section className="sa-section" aria-labelledby="sa-composer">
        <h2 id="sa-composer">{t('tnInbox.composerHeading')}</h2>
        <TaskComposer />
        <div className="sa-statusline" role="status">
          <span>{t('tnInbox.statusline.counts', { running: runningTotal, needsYou: needsYouTotal, queued: queuedTotal })}</span>
          <span>{t('tnInbox.statusline.palette')}</span>
        </div>
      </section>
    </div>
  );
}

function QueueRow({ ticket }: { ticket: AutonomyTicket }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <div className="sa-queue-row">
      <StateMark state="queued" />
      <span className="sa-queue-title">
        <button type="button" className="sa-link" onClick={() => navigate(`/tasks/${ticket.ticket_id}`)}>
          {ticket.title || ticket.origin_thread_key}
        </button>
      </span>
      <span className="sa-mono-id">{t('tnInbox.meta.queued')} <Age iso={ticket.updated_at} /></span>
    </div>
  );
}
