import { Component, Suspense, lazy, useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import i18n from 'i18next';
import { getNativeAuthStatuses } from '@/api/nativeAgents';
import { getMainAgentStatusBoard, type MainAgentAlert, type MainAgentStatus } from '@/api/mainAgent';
import {
  createAutonomyDaemon,
  deleteAutonomyDaemon,
  getAutonomyMetrics,
  listAutonomyDaemons,
  updateAutonomyDaemon,
  type AutonomyDaemon,
} from '@/api/autonomy';
import { useNativeAgents } from '@/hooks/useNativeAgents';
import { useLocale } from '@/hooks/useLocale';
import { formatTimestamp } from '@/lib/format';
import { registerFeatureLocales } from '@/lib/featureLocales';
import { nativeRuntimeLabel } from '@/lib/nativeRuntimeLabels';
import { queryKeys } from '@/lib/queryKeys';
import tnAgentsEn from './locales/tnAgents.en-US.json';
import tnAgentsZh from './locales/tnAgents.zh-CN.json';
import './agents.css';

// The Agents page keeps its copy in its own tnAgents pair beside the main
// catalogs; registering at module load makes t('tnAgents.…') resolve.
registerFeatureLocales({ 'en-US': tnAgentsEn, 'zh-CN': tnAgentsZh });

// The panel pulls its own query; loading it lazily keeps the agents page's
// first paint free of the (large) inventory payload.
const LazyFleetSkillsPanel = lazy(() => import('./FleetSkillsPanel'));

/**
 * Agent presence grammar: one glyph, one word and one token ink per state.
 * Glyphs reuse tokens.json `status` shapes (● running, ○ queued, ✖ failed).
 */
const AGENT_STATES = {
  online: { glyph: '●', ink: 'ok' },
  busy: { glyph: '●', ink: 'tempo' },
  offline: { glyph: '○', ink: 'muted' },
  stalled: { glyph: '✖', ink: 'error' },
} as const;

type AgentStateId = keyof typeof AGENT_STATES;

/** The state glyph + word for one agent row. */
export function AgentState({ state }: { state: AgentStateId }) {
  const { t } = useTranslation();
  return (
    <span className={`ag-state ag-state--${state}`} data-state={state}>
      <span className="ag-state-glyph" aria-hidden="true">{AGENT_STATES[state].glyph}</span>
      <span className="ag-state-word">{t(`tnAgents.state.${state}`)}</span>
    </span>
  );
}

/** Heartbeat window used when the status board has not reported one. */
const DEFAULT_HEARTBEAT_SECONDS = 60;

/**
 * A daemon is busy while the status board shows a run on it, online while it
 * was seen within the heartbeat threshold (measured against the time its row
 * was fetched), and offline otherwise.
 */
function daemonState(
  row: Pick<AutonomyDaemon, 'daemon_id' | 'last_seen_at'>,
  busyDaemons: ReadonlySet<string>,
  heartbeatSeconds: number,
  fetchedAt: number,
): AgentStateId {
  if (busyDaemons.has(row.daemon_id)) return 'busy';
  if (!row.last_seen_at) return 'offline';
  const seen = Date.parse(row.last_seen_at);
  if (!Number.isFinite(seen)) return 'offline';
  return fetchedAt - seen <= heartbeatSeconds * 1000 ? 'online' : 'offline';
}

export default function AgentsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const locale = useLocale();
  const [daemonName, setDaemonName] = useState('');
  const [daemonConcurrency, setDaemonConcurrency] = useState('1');
  const [daemonConcurrencyDrafts, setDaemonConcurrencyDrafts] = useState<Record<string, string>>({});
  const [issuedToken, setIssuedToken] = useState('');
  const agents = useNativeAgents(true);
  const auth = useQuery({ queryKey: ['nativeAgents', 'auth'], queryFn: getNativeAuthStatuses, staleTime: 30_000 });
  const metrics = useQuery({
    queryKey: ['autonomy', 'metrics'],
    queryFn: getAutonomyMetrics,
    refetchInterval: 5_000,
  });
  const statusBoard = useQuery({
    queryKey: queryKeys.mainAgent.statusBoard(),
    queryFn: getMainAgentStatusBoard,
    refetchInterval: 5_000,
  });
  const slots = metrics.data
    ? `${metrics.data.running_tickets} / ${metrics.data.max_workers ?? 4}`
    : null;
  const daemons = useQuery({ queryKey: queryKeys.autonomy.daemons(), queryFn: listAutonomyDaemons, refetchInterval: 15_000 });
  const createDaemon = useMutation({
    mutationFn: () => createAutonomyDaemon({ name: daemonName.trim() || 'local', provider: 'codex', max_concurrency: Number(daemonConcurrency) }),
    onSuccess: (row) => {
      setIssuedToken(row.token);
      setDaemonName('');
      setDaemonConcurrency('1');
      void qc.invalidateQueries({ queryKey: queryKeys.autonomy.daemons() });
    },
  });
  const removeDaemon = useMutation({
    mutationFn: deleteAutonomyDaemon,
    onSuccess: () => void qc.invalidateQueries({ queryKey: queryKeys.autonomy.daemons() }),
  });
  const changeConcurrency = useMutation({
    mutationFn: ({ daemonId, max_concurrency }: { daemonId: string; max_concurrency: number }) =>
      updateAutonomyDaemon(daemonId, { max_concurrency }),
    onSuccess: (_row, variables) => {
      setDaemonConcurrencyDrafts((drafts) => {
        if (drafts[variables.daemonId] !== String(variables.max_concurrency)) return drafts;
        const next = { ...drafts };
        delete next[variables.daemonId];
        return next;
      });
      void qc.invalidateQueries({ queryKey: queryKeys.autonomy.daemons() });
    },
  });
  const authByProvider = new Map((auth.data ?? []).map((row) => [row.provider, row]));
  const checking = agents.isFetching || auth.isFetching || statusBoard.isFetching;
  const busyDaemons = new Set(
    (statusBoard.data?.agents ?? []).map((run) => run.daemon_id).filter((id): id is string => !!id),
  );
  const heartbeatSeconds = statusBoard.data?.thresholds.heartbeat_seconds ?? DEFAULT_HEARTBEAT_SECONDS;
  const newConcurrencyValid = Number.isInteger(Number(daemonConcurrency))
    && Number(daemonConcurrency) >= 1
    && Number(daemonConcurrency) <= 32;
  return (
    <div className="sa-page ag-page">
      <header className="ag-head">
        <div className="ag-head-copy">
          <h1>{t('tnAgents.title')}</h1>
          <p className="ag-lede">{t('tnAgents.lede')}</p>
          {slots ? (
            <p className="ag-statusline">
              {t('tnAgents.slots')}: <b>{slots}</b>
            </p>
          ) : null}
        </div>
        <div className="ag-actions">
          <button
            type="button"
            className="ag-btn"
            onClick={() => {
              void agents.refetch();
              void auth.refetch();
              void statusBoard.refetch();
            }}
            disabled={checking}
          >
            {checking ? t('tnAgents.checking') : t('tnAgents.refresh')}
          </button>
          <button type="button" className="ag-btn ag-btn--primary" onClick={() => navigate('/settings?tab=native')}>
            {t('tnAgents.manageRuntimes')}
          </button>
        </div>
      </header>
      {agents.isError || auth.isError ? (
        <p className="sa-error">{t('tnAgents.runtimesError')}</p>
      ) : null}

      <section className="ag-section" aria-labelledby="ag-runtimes-title">
        <h2 id="ag-runtimes-title" className="ag-section-title">{t('tnAgents.runtimes.title')}</h2>
        <ul className="ag-rows">
          <li className="ag-row" data-testid="agent-row-ptc">
            <AgentState state="online" />
            <span className="ag-name">{t('tnAgents.runtimes.ptcName')}</span>
            <span className="ag-meta">{t('tnAgents.runtimes.ptcDesc')}</span>
            <span className="ag-row-actions">
              <button type="button" className="ag-link" onClick={() => navigate('/inbox?provider=ptc')}>
                {t('tnAgents.runtimes.createTask')}
              </button>
            </span>
          </li>
          {(agents.data ?? []).map((agent) => {
            const signedIn = authByProvider.get(agent.provider)?.authenticated;
            const ready = agent.installed && agent.live_verified;
            return (
              <li key={agent.provider} className="ag-row" data-testid={`agent-row-${agent.provider}`}>
                <AgentState state={ready ? 'online' : 'offline'} />
                <span className="ag-name">{nativeRuntimeLabel(agent.provider)}</span>
                <span className="ag-meta">
                  {agent.installed
                    ? (agent.live_verified ? t('tnAgents.runtimes.verified') : t('tnAgents.runtimes.pending'))
                    : t('tnAgents.runtimes.missing')}
                </span>
                <span className="ag-row-actions">
                  <span className={`ag-tag ${signedIn ? 'ag-tag--ok' : 'ag-tag--idle'}`}>
                    {signedIn ? t('tnAgents.runtimes.signedIn') : t('tnAgents.runtimes.signInNeeded')}
                  </span>
                  <button
                    type="button"
                    className="ag-link"
                    disabled={!ready}
                    onClick={() => navigate(`/inbox?provider=${encodeURIComponent(agent.provider)}`)}
                  >
                    {t('tnAgents.runtimes.createTask')}
                  </button>
                  <button type="button" className="ag-link" onClick={() => navigate('/settings?tab=native')}>
                    {t('tnAgents.runtimes.manage')}
                  </button>
                </span>
              </li>
            );
          })}
        </ul>
      </section>

      <section className="ag-section" aria-labelledby="main-agent-board-title">
        <h2 id="main-agent-board-title" className="ag-section-title">{t('tnAgents.main.title')}</h2>
        <p className="ag-lede">{t('tnAgents.main.lede')}</p>
        {statusBoard.isPending ? <p className="sa-muted" role="status">{t('tnAgents.main.loading')}</p> : null}
        {statusBoard.isError ? <p className="sa-error" role="alert">{t('tnAgents.main.error')}</p> : null}
        {statusBoard.isSuccess && statusBoard.data.agents.length === 0 ? (
          <p className="sa-muted">{t('tnAgents.main.empty')}</p>
        ) : null}
        {(statusBoard.data?.agents.length ?? 0) > 0 ? (
          <ul className="ag-rows">
            {(statusBoard.data?.agents ?? []).map((run) => (
              <MainAgentRunRow key={`${run.run_id}:${run.task_id}`} run={run} locale={locale} />
            ))}
          </ul>
        ) : null}
        {statusBoard.isSuccess && statusBoard.data.alerts.length > 0 ? (
          <div className="ag-alerts" aria-label={t('tnAgents.main.alertsLabel')}>
            <h3 className="ag-subtitle">{t('tnAgents.main.alerts')}</h3>
            {statusBoard.data.alerts.map((alert, index) => <MainAgentAlertLine key={alertKey(alert, index)} alert={alert} locale={locale} />)}
          </div>
        ) : null}
        {statusBoard.isSuccess && statusBoard.data.alerts.length === 0 ? <p className="sa-muted">{t('tnAgents.main.noAlerts')}</p> : null}
        {statusBoard.data ? (
          <p className="ag-statusline">
            {t('tnAgents.main.thresholds', {
              heartbeat: statusBoard.data.thresholds.heartbeat_seconds,
              progress: statusBoard.data.thresholds.progress_seconds,
            })}
          </p>
        ) : null}
      </section>

      <section className="ag-section" aria-labelledby="ag-daemons-title">
        <h2 id="ag-daemons-title" className="ag-section-title">{t('tnAgents.daemons.title')}</h2>
        <p className="ag-lede">{t('tnAgents.daemons.lede')}</p>
        {daemons.isSuccess && daemons.data.daemons.length === 0 ? (
          <p className="sa-muted">{t('tnAgents.daemons.empty')}</p>
        ) : null}
        {(daemons.data?.daemons.length ?? 0) > 0 ? (
          <ul className="ag-rows">
            {(daemons.data?.daemons ?? []).map((row) => {
              const label = row.name || row.daemon_id.slice(0, 8);
              const draft = daemonConcurrencyDrafts[row.daemon_id] ?? String(row.max_concurrency);
              const draftNumber = Number(draft);
              const saveDisabled = changeConcurrency.isPending
                || !Number.isInteger(draftNumber)
                || draftNumber < 1
                || draftNumber > 32
                || draftNumber === row.max_concurrency;
              return (
                <li key={row.daemon_id} className="ag-row ag-row--daemon" data-testid={`daemon-row-${row.daemon_id}`}>
                  <AgentState state={daemonState(row, busyDaemons, heartbeatSeconds, daemons.dataUpdatedAt)} />
                  <span className="ag-name">{label}</span>
                  <span className="ag-meta">
                    {row.provider || t('tnAgents.daemons.native')} ·{' '}
                    {row.last_seen_at
                      ? `${t('tnAgents.daemons.seen')} ${formatTimestamp(row.last_seen_at, locale)}`
                      : t('tnAgents.daemons.neverSeen')}
                  </span>
                  <span className="ag-row-actions">
                    <label className="ag-inline-field" htmlFor={`daemon-concurrency-${row.daemon_id}`}>
                      <span className="ag-field-label">{t('tnAgents.daemons.maxFor', { name: label })}</span>
                      <input
                        type="number"
                        min="1"
                        max="32"
                        className="ag-input ag-input--num"
                        id={`daemon-concurrency-${row.daemon_id}`}
                        disabled={changeConcurrency.isPending}
                        value={draft}
                        onChange={(event) => setDaemonConcurrencyDrafts((drafts) => ({ ...drafts, [row.daemon_id]: event.target.value }))}
                      />
                    </label>
                    <button
                      type="button"
                      className="ag-btn"
                      disabled={saveDisabled}
                      onClick={() => changeConcurrency.mutate({ daemonId: row.daemon_id, max_concurrency: draftNumber })}
                    >
                      {changeConcurrency.isPending ? t('tnAgents.daemons.saving') : t('tnAgents.daemons.save')}
                    </button>
                    <button type="button" className="ag-btn ag-btn--danger" onClick={() => removeDaemon.mutate(row.daemon_id)}>
                      {t('tnAgents.daemons.remove')}
                    </button>
                  </span>
                </li>
              );
            })}
          </ul>
        ) : null}
        <div className="ag-form">
          <label className="ag-field">
            <span className="ag-field-label">{t('tnAgents.daemons.nameLabel')}</span>
            <input className="ag-input" value={daemonName} onChange={(e) => setDaemonName(e.target.value)} placeholder="my-laptop" />
          </label>
          <label className="ag-field" htmlFor="new-daemon-concurrency">
            <span className="ag-field-label">{t('tnAgents.daemons.maxLabel')}</span>
            <input
              id="new-daemon-concurrency"
              className="ag-input ag-input--num"
              type="number"
              min="1"
              max="32"
              value={daemonConcurrency}
              onChange={(e) => setDaemonConcurrency(e.target.value)}
            />
          </label>
          <button
            type="button"
            className="ag-btn ag-btn--primary"
            disabled={createDaemon.isPending || !newConcurrencyValid}
            onClick={() => createDaemon.mutate()}
          >
            {createDaemon.isPending ? t('tnAgents.daemons.creating') : t('tnAgents.daemons.create')}
          </button>
        </div>
        {issuedToken ? (
          <p className="ag-token-line">
            <span className="ag-field-label">{t('tnAgents.daemons.token')}</span>{' '}
            <code className="ag-token">{issuedToken}</code>{' '}
            <button type="button" className="ag-link" onClick={() => void navigator.clipboard?.writeText(issuedToken)}>
              {t('tnAgents.daemons.copy')}
            </button>
          </p>
        ) : null}
        {createDaemon.isError ? <p className="sa-error">{t('tnAgents.daemons.createError')}</p> : null}
        {changeConcurrency.isError ? <p className="sa-error">{t('tnAgents.daemons.updateError')}</p> : null}
      </section>

      <FleetSkillsSection />
    </div>
  );
}

/** Collapsible read-only view of every fleet CLI's skill directory. */
function FleetSkillsSection() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <section className="ag-section" aria-labelledby="ag-fleet-title">
      <h2 id="ag-fleet-title" className="ag-section-title">{t('tnAgents.fleet.title')}</h2>
      <p className="ag-lede">
        {t('tnAgents.fleet.lede')} <code className="ag-code">conductor skills</code>.
      </p>
      <div className="ag-actions">
        <button type="button" className="ag-btn" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
          {open ? t('tnAgents.fleet.hide') : t('tnAgents.fleet.show')}
        </button>
      </div>
      {open ? (
        <FleetSkillsPanelBoundary>
          <Suspense fallback={<p className="sa-muted">{t('tnAgents.fleet.loadingChunk')}</p>}>
            <LazyFleetSkillsPanel />
          </Suspense>
        </FleetSkillsPanelBoundary>
      ) : null}
    </section>
  );
}

interface BoundaryState {
  /** Whether anything was thrown, which `error` alone cannot say — a rejected
   *  chunk loader may hand us `null` or `undefined`. */
  failed: boolean;
}

/**
 * Keeps a failed lazy-chunk load inside the fleet-skills section.
 *
 * `<Suspense>` handles pending promises, not rejections: without a boundary
 * a chunk a deploy deleted (or any render error in the panel) walks to the
 * route-level `StaleBuildBoundary` and replaces the whole Agents page. The
 * panel is one optional section — it fails as an inline notice with a
 * retry, and the error is logged so a real regression is not masked.
 */
export class FleetSkillsPanelBoundary extends Component<{ children: ReactNode }, BoundaryState> {
  override state: BoundaryState = { failed: false };

  static getDerivedStateFromError(): BoundaryState {
    return { failed: true };
  }

  override componentDidCatch(error: unknown): void {
    console.error('[FleetSkillsPanel]', error);
  }

  override render(): ReactNode {
    if (this.state.failed) {
      // A class component has no hook; read the active catalog directly.
      return (
        <p className="sa-error" role="alert">
          {i18n.t('tnAgents.fleet.chunkError')}{' '}
          <button type="button" className="ag-link" onClick={() => this.setState({ failed: false })}>
            {i18n.t('tnAgents.fleet.retry')}
          </button>
        </p>
      );
    }
    return this.props.children;
  }
}

function MainAgentRunRow({ run, locale }: { run: MainAgentStatus; locale: string }) {
  const { t } = useTranslation();
  return (
    <li className="ag-row ag-row--run" data-testid={`main-agent-run-${run.run_id}-${run.task_id}`}>
      <AgentState state={run.stalled ? 'stalled' : 'busy'} />
      <span className="ag-name">{run.agent_id || t('tnAgents.main.worker')}</span>
      <span className="ag-meta">
        <span className="ag-run-state">{run.state}</span>
        {' · '}{t('tnAgents.main.run')} {run.run_id} · {t('tnAgents.main.task')} {run.task_id}
        {run.ticket_id ? ` · ${t('tnAgents.main.ticket')} ${run.ticket_id}` : ''}
        {run.daemon_id ? ` · ${t('tnAgents.main.daemon')} ${run.daemon_id}` : ''}
      </span>
      <span className="ag-run-detail">
        <span className="ag-step">
          <span className="ag-field-label">{t('tnAgents.main.currentStep')}</span>{' '}
          <span>{run.current_step || t('tnAgents.main.waitingStep')}</span>
        </span>
        <span className="ag-heartbeat">
          <span className="ag-field-label">{t('tnAgents.main.lastHeartbeat')}</span>{' '}
          {run.last_heartbeat ? formatTimestamp(run.last_heartbeat, locale) : t('tnAgents.main.notReported')}
        </span>
        {run.stalled && run.stall_reason ? <span className="ag-stall">{run.stall_reason}</span> : null}
      </span>
    </li>
  );
}

function MainAgentAlertLine({ alert, locale }: { alert: MainAgentAlert | string; locale: string }) {
  const { t } = useTranslation();
  if (typeof alert === 'string') return <p className="ag-alert">{alert}</p>;
  return (
    <p className="ag-alert">
      {alert.severity ? <strong>{alert.severity}: </strong> : null}
      {alert.message}
      {alert.run_id ? ` · ${t('tnAgents.main.run').toLowerCase()} ${alert.run_id}` : ''}
      {alert.created_at ? ` · ${formatTimestamp(alert.created_at, locale)}` : ''}
    </p>
  );
}

function alertKey(alert: MainAgentAlert | string, index: number): string {
  if (typeof alert === 'string') return `${index}-${alert}`;
  return alert.id || `${index}-${alert.run_id ?? ''}-${alert.task_id ?? ''}-${alert.created_at ?? ''}`;
}
