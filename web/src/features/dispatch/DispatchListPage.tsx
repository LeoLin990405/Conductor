import { useEffect, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';
import { getDispatch, listActiveDispatches, startDispatch } from '@/api/dispatch';
import { apiErrorReason } from '@/api/core/api/errors';
import {
  clip,
  readDispatchHistory,
  rememberDispatch,
  statusMark,
  type DispatchSummary,
} from './model';
import './dispatch.css';

export default function DispatchListPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [goal, setGoal] = useState('');
  const [draft, setDraft] = useState('');
  const [startError, setStartError] = useState('');
  const [historyIds, setHistoryIds] = useState<string[]>(() => readDispatchHistory());

  const active = useQuery({
    queryKey: ['dispatch', 'active'],
    queryFn: listActiveDispatches,
    refetchInterval: () => (document.visibilityState === 'hidden' ? false : 5_000),
  });

  // The history is the dispatches this user has seen or started. There is no
  // route that lists them, so each one is read back through the one route
  // that reads a dispatch.
  const history = useQuery({
    queryKey: ['dispatch', 'history', historyIds.join(',')],
    queryFn: async () => {
      const rows = await Promise.all(
        historyIds.map((id) => getDispatch(id).catch(() => null)),
      );
      return rows.filter((row): row is DispatchSummary => Boolean(row));
    },
    enabled: historyIds.length > 0,
  });

  const { refetch } = active;

  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === 'visible') void refetch();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, [refetch]);

  const start = useMutation({
    mutationFn: (value: string) => startDispatch(value),
    onSuccess: (summary) => {
      setStartError('');
      setGoal('');
      if (summary?.id) {
        setHistoryIds(rememberDispatch(summary.id));
        void client.invalidateQueries({ queryKey: ['dispatch', 'active'] });
        navigate(`/dispatch/${encodeURIComponent(summary.id)}`);
      }
    },
    onError: (err) => {
      // The backend refuses a start with the reason it refused it (a planner
      // that is switched off, a goal over the planner's ceiling). The reason
      // is the whole answer, so it is shown instead of a generic sentence.
      setStartError(apiErrorReason(err) ?? t('tnDispatch.startFailed'));
    },
  });

  const submitStart = (event: FormEvent) => {
    event.preventDefault();
    const value = goal.trim();
    if (!value || start.isPending) return;
    start.mutate(value);
  };

  const openById = (event: FormEvent) => {
    event.preventDefault();
    const id = draft.trim();
    if (id) navigate(`/dispatch/${encodeURIComponent(id)}`);
  };

  const openRow = (row: DispatchSummary) => {
    if (!row.id) return;
    setHistoryIds(rememberDispatch(row.id));
    navigate(`/dispatch/${encodeURIComponent(row.id)}`);
  };

  return (
    <div className="tn-dispatch">
      <header className="tn-dispatch-head">
        <h1>{t('tnDispatch.title')}</h1>
        <p className="tn-dispatch-sub">{t('tnDispatch.subtitle')}</p>
        <p className="tn-dispatch-lede">{t('tnDispatch.lede')}</p>
      </header>

      <section className="tn-dispatch-section" aria-labelledby="tn-dispatch-start">
        <h2 id="tn-dispatch-start">{t('tnDispatch.start')}</h2>
        <form className="tn-dispatch-card tn-dispatch-form" onSubmit={submitStart}>
          <label className="tn-dispatch-field">
            <span>{t('tnDispatch.goal')}</span>
            <textarea
              value={goal}
              onChange={(event) => setGoal(event.target.value)}
              placeholder={t('tnDispatch.goalPlaceholder')}
              rows={3}
            />
          </label>
          <button
            type="submit"
            className="tn-dispatch-btn tn-dispatch-btn--primary"
            disabled={start.isPending || !goal.trim()}
          >
            {start.isPending ? t('tnDispatch.starting') : t('tnDispatch.startAction')}
          </button>
        </form>
        {startError ? (
          <p className="tn-dispatch-alert" role="alert">
            {startError}
          </p>
        ) : null}
      </section>

      <div className="tn-dispatch-columns">
        <section className="tn-dispatch-section" aria-labelledby="tn-dispatch-active">
          <h2 id="tn-dispatch-active">
            {t('tnDispatch.active')}
            <span className="tn-dispatch-mono">
              {t('tnDispatch.countOf', { count: active.data?.length ?? 0 })}
            </span>
          </h2>
          <p className="tn-dispatch-hint">{t('tnDispatch.activeHint')}</p>
          {active.isPending ? <p className="tn-dispatch-note">{t('tnDispatch.loading')}</p> : null}
          {active.isError ? (
            <p className="tn-dispatch-alert" role="alert">
              {t('tnDispatch.error')}
            </p>
          ) : null}
          {active.isSuccess && active.data.length === 0 ? (
            <p className="tn-dispatch-note">{t('tnDispatch.empty')}</p>
          ) : null}
          <ul className="tn-dispatch-list">
            {(active.data ?? []).map((row) => (
              <DispatchRow key={row.id ?? row.goal} row={row} onOpen={() => openRow(row)} />
            ))}
          </ul>
        </section>

        <section className="tn-dispatch-section" aria-labelledby="tn-dispatch-history">
          <h2 id="tn-dispatch-history">
            {t('tnDispatch.history')}
            <span className="tn-dispatch-mono">
              {t('tnDispatch.countOf', { count: history.data?.length ?? 0 })}
            </span>
          </h2>
          <p className="tn-dispatch-hint">{t('tnDispatch.historyHint')}</p>
          {historyIds.length === 0 ? (
            <p className="tn-dispatch-note">{t('tnDispatch.historyEmpty')}</p>
          ) : null}
          {history.isError ? (
            <p className="tn-dispatch-alert" role="alert">
              {t('tnDispatch.error')}
            </p>
          ) : null}
          <ul className="tn-dispatch-list">
            {(history.data ?? []).map((row) => (
              <DispatchRow key={row.id ?? row.goal} row={row} onOpen={() => openRow(row)} />
            ))}
          </ul>
        </section>
      </div>

      <section className="tn-dispatch-section" aria-labelledby="tn-dispatch-open">
        <h2 id="tn-dispatch-open">{t('tnDispatch.openById')}</h2>
        <form className="tn-dispatch-card tn-dispatch-form" onSubmit={openById}>
          <label className="tn-dispatch-field">
            <span>{t('tnDispatch.idPlaceholder')}</span>
            <input
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder={t('tnDispatch.idPlaceholder')}
            />
          </label>
          <button type="submit" className="tn-dispatch-btn" disabled={!draft.trim()}>
            {t('tnDispatch.open')}
          </button>
        </form>
      </section>
    </div>
  );
}

function DispatchRow({ row, onOpen }: { row: DispatchSummary; onOpen: () => void }) {
  const { t } = useTranslation();
  const count = Array.isArray(row.lines) ? row.lines.length : 0;
  const mark = statusMark(row.state);
  return (
    <li className="tn-dispatch-row">
      <div className="tn-dispatch-row-top">
        <button type="button" className="tn-dispatch-link" onClick={onOpen}>
          {row.id || t('tnDispatch.dash')}
        </button>
        <span className={`tn-dispatch-st tn-dispatch-st--${mark.tone}`}>
          <span className="tn-dispatch-glyph" aria-hidden="true">
            {mark.glyph}
          </span>
          <span className="tn-dispatch-word">{t(`tnDispatch.status.${mark.id}`)}</span>
        </span>
        <span className="tn-dispatch-mono">
          {typeof row.progress_pct === 'number' ? `${row.progress_pct}%` : t('tnDispatch.dash')}
        </span>
        <span className="tn-dispatch-mono">{t('tnDispatch.linesCount', { count })}</span>
      </div>
      <span className="tn-dispatch-row-goal">{clip(row.goal, 200) || t('tnDispatch.dash')}</span>
    </li>
  );
}
