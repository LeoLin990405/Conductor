import { useEffect, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useParams, useSearchParams } from 'react-router';
import ConfirmDialog from '@/components/ConfirmDialog';
import {
  approveDispatch,
  getDispatch,
  getProviderCatalog,
  reassignDispatchLine,
  rejectDispatch,
} from '@/api/dispatch';
import { apiErrorReason } from '@/api/core/api/errors';
import {
  TERMINAL_STATES,
  clip,
  focusRecord,
  isAwaitingHuman,
  lineActors,
  lineId,
  linePhase,
  lineVerdict,
  forgetDispatch,
  providerOf,
  queuedReassign,
  rememberDispatch,
  reviewedCommit,
  statusMark,
  timelineOf,
  tokenIssued,
  verdictRows,
  type DispatchLine,
  type DispatchSummary,
  type ProviderCatalog,
} from './model';
import './dispatch.css';

type Decision = 'approve' | 'reject';

export default function DispatchDetailPage() {
  const { t } = useTranslation();
  const { dispatchId = '' } = useParams();
  const id = decodeURIComponent(dispatchId);
  const client = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get('confirm');
  const [asked, setAsked] = useState<Decision | null>(null);
  const [note, setNote] = useState('');
  const [conflict, setConflict] = useState('');
  const [failure, setFailure] = useState('');
  const [busy, setBusy] = useState(false);

  const detail = useQuery({
    queryKey: ['dispatch', id],
    queryFn: () => getDispatch(id),
    enabled: Boolean(id),
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: (query) => {
      if (document.visibilityState === 'hidden') return false;
      const row = query.state.data;
      if (!row || TERMINAL_STATES.has(String(row.state || ''))) return false;
      return 5_000;
    },
  });

  // Which provider serves which model. A line names its model, never its
  // provider, so the catalog is the only authority for the pair.
  const catalog = useQuery({
    queryKey: ['dispatch', 'providers'],
    queryFn: getProviderCatalog,
    staleTime: 300_000,
    retry: false,
  });

  const { data: summary, isPending, isError, refetch } = detail;
  const awaiting = isAwaitingHuman(summary);
  const providerCatalog: ProviderCatalog = catalog.data ?? { byModel: {}, providers: [] };

  // The user has seen this dispatch: it belongs in their history. An id the
  // backend does not serve (a 404 reads as null) is dropped instead, so dead
  // ids never take a history slot.
  const loaded = detail.isSuccess;
  const servedId = summary?.id ? id : null;
  useEffect(() => {
    if (!id || !loaded) return;
    if (servedId) rememberDispatch(servedId);
    else forgetDispatch(id);
  }, [id, loaded, servedId]);

  // `?confirm=` is a one-shot request. On a dispatch that is not waiting for
  // a person it is ignored, and dropped from the URL, so the dialog never
  // opens by itself later when the dispatch reaches APPROVAL.
  useEffect(() => {
    if (!loaded || awaiting || !searchParams.has('confirm')) return;
    const next = new URLSearchParams(searchParams);
    next.delete('confirm');
    setSearchParams(next, { replace: true });
  }, [loaded, awaiting, searchParams, setSearchParams]);

  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState !== 'visible') return;
      const row = client.getQueryData<DispatchSummary | null>(['dispatch', id]);
      if (!row || TERMINAL_STATES.has(String(row.state || ''))) return;
      void refetch();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, [client, id, refetch]);

  /**
   * The decision the confirmation dialog is about.
   *
   * `?confirm=approve|reject` opens the dialog for this id and nothing else:
   * the decision is never taken from the query string alone. The operator's
   * own button wins over the query string. A dispatch that stops waiting for
   * a person closes the dialog, because there is nothing left to decide.
   */
  const askedByQuery: Decision | null =
    requested === 'approve' || requested === 'reject' ? requested : null;
  const pending: Decision | null = awaiting ? (asked ?? askedByQuery) : null;

  const closeDialog = () => {
    setAsked(null);
    if (searchParams.has('confirm')) {
      const next = new URLSearchParams(searchParams);
      next.delete('confirm');
      setSearchParams(next, { replace: true });
    }
  };

  const decide = async (action: Decision) => {
    if (busy) return;
    const current = client.getQueryData<DispatchSummary | null>(['dispatch', id]);
    if (!current || !isAwaitingHuman(current)) {
      closeDialog();
      await refetch();
      return;
    }
    setBusy(true);
    setConflict('');
    setFailure('');
    try {
      if (action === 'approve') await approveDispatch(id, note);
      else await rejectDispatch(id, note);
      setNote('');
      closeDialog();
      await client.invalidateQueries({ queryKey: ['dispatch', id] });
    } catch (err) {
      const status = (err as { status?: number; response?: { status?: number } }).response?.status
        ?? (err as { status?: number }).status;
      if (status === 409) {
        setConflict(t('tnDispatch.conflict'));
        closeDialog();
        await refetch();
      } else {
        setFailure(t('tnDispatch.actionFailed'));
      }
    } finally {
      setBusy(false);
    }
  };

  /**
   * The reassign refusal, on the line it belongs to.
   *
   * A refusal is about one line: the backend names the reason it refused
   * (`reassign_pending`, a reserved role, a line that is not reassignable).
   * The reason stays on that line's form and the model the operator typed
   * stays in the field, so the fix is one edit away.
   */
  const [reassignError, setReassignError] = useState<{ line: string; message: string } | null>(null);

  const reassign = useMutation({
    mutationFn: (input: { line: string; model: string }) =>
      reassignDispatchLine(id, input.line, input.model),
    onSuccess: (next) => {
      setReassignError(null);
      // The answer is the dispatch as it is now: a queued reassign shows on
      // its line straight away, before the next poll.
      if (next && typeof next === 'object' && next.id) client.setQueryData(['dispatch', id], next);
      void client.invalidateQueries({ queryKey: ['dispatch', id] });
    },
    onError: (err, variables) => {
      setReassignError({
        line: variables.line,
        message: apiErrorReason(err) ?? t('tnDispatch.reassignFailed'),
      });
    },
  });

  /** Reassign one line; true when the backend took it. */
  const doReassign = async (lineKey: string, model: string): Promise<boolean> => {
    try {
      await reassign.mutateAsync({ line: lineKey, model });
      return true;
    } catch {
      // The refusal is already on the line; the caller keeps the model.
      return false;
    }
  };

  if (isPending) return <p className="tn-dispatch-note">{t('tnDispatch.loading')}</p>;
  if (isError) {
    return (
      <p className="tn-dispatch-alert" role="alert">
        {t('tnDispatch.error')}
      </p>
    );
  }
  if (!summary) return <p className="tn-dispatch-note">{t('tnDispatch.notFound')}</p>;

  const dash = t('tnDispatch.dash');
  const mark = statusMark(summary.state);
  const lines = summary.lines ?? [];
  const record = focusRecord(summary);
  const axis = timelineOf(record);
  const rows = verdictRows(summary);

  return (
    <div className="tn-dispatch">
      <header className="tn-dispatch-head">
        <h1>{summary.id || dash}</h1>
        <p className="tn-dispatch-sub">{t('tnDispatch.subtitle')}</p>
        <div className="tn-dispatch-meta">
          <span className={`tn-dispatch-st tn-dispatch-st--${mark.tone}`}>
            <span className="tn-dispatch-glyph" aria-hidden="true">
              {mark.glyph}
            </span>
            <span className="tn-dispatch-word">{t(`tnDispatch.status.${mark.id}`)}</span>
          </span>
          <span className="tn-dispatch-mono">
            {t('tnDispatch.progress')} {typeof summary.progress_pct === 'number' ? `${summary.progress_pct}%` : dash}
          </span>
          <span className="tn-dispatch-mono">{summary.integrate_branch || dash}</span>
          {tokenIssued(summary) ? (
            <span className="tn-dispatch-mono">{t('tnDispatch.tokenIssued')}</span>
          ) : null}
        </div>
        <p className="tn-dispatch-lede">{clip(summary.goal, 400) || dash}</p>
        {summary.message ? <p className="tn-dispatch-hint">{clip(summary.message, 300)}</p> : null}
      </header>

      {conflict ? (
        <p className="tn-dispatch-alert" role="alert">
          {conflict}
        </p>
      ) : null}
      {failure ? (
        <p className="tn-dispatch-alert" role="alert">
          {failure}
        </p>
      ) : null}

      <section className="tn-dispatch-section" aria-labelledby="tn-dispatch-lines">
        <h2 id="tn-dispatch-lines">
          {t('tnDispatch.lines')}
          <span className="tn-dispatch-mono">{t('tnDispatch.linesRange')}</span>
        </h2>
        {lines.length === 0 ? <p className="tn-dispatch-note">{dash}</p> : null}
        <div className="tn-dispatch-grid-4">
          {lines.map((line, index) => (
            <LineCard
              key={lineId(line) || line.name || index}
              summary={summary}
              line={line}
              index={index}
              catalog={providerCatalog}
              onReassign={(lineKey, model) => doReassign(lineKey, model)}
              error={reassignError?.line === lineId(line) ? reassignError.message : null}
            />
          ))}
        </div>
      </section>

      <section className="tn-dispatch-section" aria-labelledby="tn-dispatch-timeline">
        <h2 id="tn-dispatch-timeline">{t('tnDispatch.timeline')}</h2>
        <div className="tn-dispatch-card">
          <ol className="tn-dispatch-axis" aria-label={t('tnDispatch.timeline')}>
            {axis.map((slot) => (
              <li key={slot.id} data-status={axisStatus(slot.status, slot.current)}>
                <span className="tn-dispatch-slot">{t(`tnDispatch.gates.${slot.id}`)}</span>
                <span className="tn-dispatch-mono">{slotText(t, slot.status)}</span>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section className="tn-dispatch-section" aria-labelledby="tn-dispatch-verdicts">
        <h2 id="tn-dispatch-verdicts">{t('tnDispatch.verdicts')}</h2>
        <div className="tn-dispatch-card">
          {rows.length === 0 ? (
            <p className="tn-dispatch-note">{dash}</p>
          ) : (
            <table className="tn-dispatch-table">
              <thead>
                <tr>
                  <th scope="col">{t('tnDispatch.line')}</th>
                  <th scope="col">{t('tnDispatch.gate')}</th>
                  <th scope="col">{t('tnDispatch.reviewer')}</th>
                  <th scope="col">{t('tnDispatch.verdict')}</th>
                  <th scope="col">{t('tnDispatch.pinned')}</th>
                  <th scope="col">{t('tnDispatch.findings')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={`${row.lineId}-${row.gate}-${row.round}`}>
                    <td data-label={t('tnDispatch.line')} className="tn-dispatch-mono">
                      {row.lineId}
                    </td>
                    <td data-label={t('tnDispatch.gate')}>
                      {t(`tnDispatch.gates.${row.gate === 'secondary_review' ? 'secondary' : 'primary'}`)}
                      {' '}
                      <span className="tn-dispatch-mono">{t('tnDispatch.round', { n: row.round })}</span>
                    </td>
                    <td data-label={t('tnDispatch.reviewer')}>
                      <ProviderModel
                        provider={providerOf(row.reviewer, providerCatalog)}
                        model={row.reviewer}
                        unknown={t('tnDispatch.unknownProvider')}
                        dash={dash}
                      />
                    </td>
                    <td data-label={t('tnDispatch.verdict')}>
                      <VerdictMark
                        tone={row.verdict.tone}
                        glyph={row.verdict.glyph}
                        word={t(`tnDispatch.verdictWord.${row.verdict.id}`)}
                      />
                    </td>
                    <td data-label={t('tnDispatch.pinned')} className="tn-dispatch-mono">
                      {reviewedCommit(gateRecordOf(summary, row.lineId)) || dash}
                    </td>
                    <td data-label={t('tnDispatch.findings')} className="tn-dispatch-mono">
                      {t('tnDispatch.blocking', { n: row.blockerCount })}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <section className="tn-dispatch-section" aria-labelledby="tn-dispatch-decide">
        <h2 id="tn-dispatch-decide">
          {t('tnDispatch.decide')}
          <span className="tn-dispatch-mono">{t('tnDispatch.decideHint')}</span>
        </h2>
        <div className="tn-dispatch-card">
          <p className="tn-dispatch-ask">
            {t('tnDispatch.approveMessage')} {t('tnDispatch.rejectMessage')}
          </p>
          <label className="tn-dispatch-field">
            <span>{t('tnDispatch.note')}</span>
            <input
              value={note}
              onChange={(event) => setNote(event.target.value)}
              placeholder={t('tnDispatch.notePlaceholder')}
            />
          </label>
          {awaiting ? (
            <div className="tn-dispatch-verb">
              <button
                type="button"
                className="tn-dispatch-btn tn-dispatch-btn--primary"
                onClick={() => setAsked('approve')}
              >
                {t('tnDispatch.approve')} <code>{summary.id || dash}</code>
              </button>
              <button
                type="button"
                className="tn-dispatch-btn tn-dispatch-btn--danger"
                onClick={() => setAsked('reject')}
              >
                {t('tnDispatch.reject')} <code>{summary.id || dash}</code>
              </button>
            </div>
          ) : (
            <p className="tn-dispatch-note">{t('tnDispatch.notAwaiting')}</p>
          )}
        </div>
      </section>

      <ConfirmDialog
        open={pending !== null}
        title={pending === 'reject' ? t('tnDispatch.rejectTitle') : t('tnDispatch.approveTitle')}
        message={
          pending === 'reject' ? t('tnDispatch.rejectMessage') : t('tnDispatch.approveMessage')
        }
        confirmLabel={pending === 'reject' ? t('tnDispatch.confirmReject') : t('tnDispatch.confirmApprove')}
        onConfirm={() => {
          if (pending) void decide(pending);
        }}
        onOpenChange={(open) => {
          if (!open) closeDialog();
        }}
      />
    </div>
  );
}

function LineCard({
  summary,
  line,
  index,
  catalog,
  onReassign,
  error,
}: {
  summary: DispatchSummary;
  line: DispatchLine;
  index: number;
  catalog: ProviderCatalog;
  onReassign: (lineId: string, model: string) => Promise<boolean>;
  error: string | null;
}) {
  const { t } = useTranslation();
  const dash = t('tnDispatch.dash');
  const id = lineId(line);
  const phase = linePhase(summary, line);
  const mark = statusMark(phase || line.status);
  const verdict = lineVerdict(gateRecordOf(summary, id));
  const actors = lineActors(summary, line, catalog);

  return (
    <article className="tn-dispatch-card tn-dispatch-line">
      <div className="tn-dispatch-line-head">
        <span className="tn-dispatch-line-name">L{index + 1}</span>
        <span className="tn-dispatch-mono">{line.name || id}</span>
        <span className={`tn-dispatch-st tn-dispatch-st--${mark.tone}`}>
          <span className="tn-dispatch-glyph" aria-hidden="true">
            {mark.glyph}
          </span>
          <span className="tn-dispatch-word">{t(`tnDispatch.status.${mark.id}`)}</span>
        </span>
      </div>
      {line.branch ? <div className="tn-dispatch-branch">{line.branch}</div> : null}
      <dl className="tn-dispatch-kv">
        <dt>{t('tnDispatch.worker')}</dt>
        <dd>
          <ProviderModel
            provider={actors.worker.provider}
            model={actors.worker.model}
            unknown={t('tnDispatch.unknownProvider')}
            dash={dash}
          />
        </dd>
        <dt>{t('tnDispatch.reviewer')}</dt>
        <dd>
          {actors.reviewer ? (
            <ProviderModel
              provider={actors.reviewer.provider}
              model={actors.reviewer.model}
              unknown={t('tnDispatch.unknownProvider')}
              dash={dash}
            />
          ) : (
            dash
          )}
        </dd>
        <dt>{t('tnDispatch.verdict')}</dt>
        <dd>
          <VerdictMark
            tone={verdict.tone}
            glyph={verdict.glyph}
            word={t(`tnDispatch.verdictWord.${verdict.id}`)}
          />
        </dd>
        <dt>{t('tnDispatch.attempts')}</dt>
        <dd className="tn-dispatch-mono">{typeof line.attempts === 'number' ? line.attempts : dash}</dd>
      </dl>
      <ReassignForm
        lineId={id}
        dash={dash}
        error={error}
        queued={queuedReassign(summary, id)}
        onReassign={(model) => onReassign(id, model)}
      />
    </article>
  );
}

function ReassignForm({
  lineId,
  dash,
  error,
  queued,
  onReassign,
}: {
  lineId: string;
  dash: string;
  error: string | null;
  queued: string | null;
  onReassign: (model: string) => Promise<boolean>;
}) {
  const { t } = useTranslation();
  const [model, setModel] = useState('');

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const value = model.trim();
    if (!value) return;
    const taken = await onReassign(value);
    // The model stays in the field when the backend refused it: the refusal
    // names the reason, and the operator's edit is still worth keeping.
    if (taken) setModel('');
  };

  return (
    <form className="tn-dispatch-reassign" onSubmit={submit}>
      <label className="tn-dispatch-field">
        <span>{t('tnDispatch.reassignModel')}</span>
        <input
          value={model}
          onChange={(event) => setModel(event.target.value)}
          placeholder={t('tnDispatch.reassignPlaceholder')}
          aria-label={`${t('tnDispatch.reassignModel')} ${lineId || dash}`}
        />
      </label>
      <button type="submit" className="tn-dispatch-btn tn-dispatch-btn--sm" disabled={!model.trim()}>
        {t('tnDispatch.reassign')}
      </button>
      {queued ? (
        <span className="tn-dispatch-hint" role="status">
          {t('tnDispatch.reassignQueued', { model: queued })}
        </span>
      ) : null}
      {error ? (
        <span className="tn-dispatch-alert" role="alert">
          {error}
        </span>
      ) : null}
    </form>
  );
}

function ProviderModel({
  provider,
  model,
  unknown,
  dash,
}: {
  provider: string;
  model: string;
  unknown: string;
  dash: string;
}) {
  if (!model) return <span className="tn-dispatch-mono">{dash}</span>;
  return (
    <span>
      <span className="tn-dispatch-provider">{provider || unknown}</span>{' '}
      <span className="tn-dispatch-mono">{model}</span>
    </span>
  );
}

function VerdictMark({ tone, glyph, word }: { tone: string; glyph: string; word: string }) {
  return (
    <span className={`tn-dispatch-verdict tn-dispatch-verdict--${tone}`}>
      <span className="tn-dispatch-mark" aria-hidden="true">
        {glyph}
      </span>
      <span className="tn-dispatch-word">{word}</span>
    </span>
  );
}

function axisStatus(status: string, current: boolean): 'done' | 'current' | 'unreached' {
  if (current) return 'current';
  if (status === 'unreached' || status === 'not-applicable') return 'unreached';
  return 'done';
}

function slotText(t: (key: string) => string, status: string): string {
  const known = [
    'unreached',
    'pending-approval',
    'current',
    'not-applicable',
    'fixing',
    'merged',
    'cancelled',
    'blocked',
    'replanning',
    'pass',
    'fail',
    'running',
    'skipped',
  ];
  if (known.includes(status)) return t(`tnDispatch.slotState.${status}`);
  return status || t('tnDispatch.dash');
}

function gateRecordOf(summary: DispatchSummary, id: string) {
  return summary.gates?.lines?.[id] ?? null;
}
