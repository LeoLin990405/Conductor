import { useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAllWorkspaces } from '@/hooks/useAllWorkspaces';
import { useNativeAgents } from '@/hooks/useNativeAgents';
import { registerFeatureLocales } from '@/lib/featureLocales';
import { nativeRuntimeLabel } from '@/lib/nativeRuntimeLabels';
import { queryKeys } from '@/lib/queryKeys';
import {
  getUnavailableEvals,
  requeueEval,
  getAutonomyPolicy,
  updateAutonomyPolicy,
  createAutonomySchedule,
  deleteAutonomySchedule,
  listAutonomySchedules,
  runAutonomySchedule,
  updateAutonomySchedule,
  listAutonomyDaemons,
  listAutonomySources,
  listAutonomyTickets,
  createAutonomySource,
  updateAutonomySource,
  checkAutonomySource,
  pollAutonomySource,
  deleteAutonomySource,
  type AutonomySchedule,
  type AutonomySource,
  type AutonomyTicket,
} from '@/api/autonomy';
import tnSourcesEn from './locales/tnSources.en-US.json';
import tnSourcesZh from './locales/tnSources.zh-CN.json';
import './sources.css';

// Sources copy lives in its own tnSources pair (registered at module load, so
// SchedulePanel also renders its copy when a test mounts it on its own).
registerFeatureLocales({ 'en-US': tnSourcesEn, 'zh-CN': tnSourcesZh });

function timezone(): string {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch { return 'UTC'; }
}

// ───────────────────────── health: glyph + word ─────────────────────────

type SourceHealth = 'ok' | 'error' | 'paused' | 'waiting' | 'active';

const HEALTH_GLYPH: Record<SourceHealth, string> = {
  ok: '✔',
  active: '✔',
  error: '✖',
  paused: '⊘',
  waiting: '○',
};

/** A polled Source's health: an error wins, then disabled, then never polled. */
function sourceHealth(source: Pick<AutonomySource, 'enabled' | 'state'>): SourceHealth {
  if (source.state?.last_error) return 'error';
  if (!source.enabled) return 'paused';
  if (!source.state?.last_poll_at) return 'waiting';
  return 'ok';
}

function scheduleHealth(row: Pick<AutonomySchedule, 'enabled' | 'last_error'>): SourceHealth {
  if (row.last_error) return 'error';
  if (!row.enabled) return 'paused';
  return 'active';
}

function HealthMark({ health }: { health: SourceHealth }) {
  const { t } = useTranslation();
  return (
    <span className={`tn-sources-health tn-sources-health--${health}`} data-health={health}>
      <span className="tn-sources-health-glyph" aria-hidden="true">{HEALTH_GLYPH[health]}</span>
      <span className="tn-sources-health-word">{t(`tnSources.health.${health}`)}</span>
    </span>
  );
}

// ───────────────────────── job counts ─────────────────────────

/** The row cap listAutonomyTickets asks for; a list this long is a lower bound. */
const TICKET_PAGE_CAP = 200;

interface JobCounts {
  running: number;
  queued: number;
  needsYou: number;
  failed: number;
  total: number;
  capped: boolean;
}

const COUNT_COLUMNS = [
  { key: 'running', glyph: '●' },
  { key: 'queued', glyph: '○' },
  { key: 'needsYou', glyph: '◆' },
  { key: 'failed', glyph: '✖' },
] as const;

/** Bucket Tickets the Inbox way: a question or a block needs you. */
function countJobs(tickets: Pick<AutonomyTicket, 'status' | 'attention'>[]): JobCounts {
  const out: JobCounts = { running: 0, queued: 0, needsYou: 0, failed: 0, total: tickets.length, capped: tickets.length >= TICKET_PAGE_CAP };
  for (const ticket of tickets) {
    if (ticket.attention) { out.needsYou += 1; continue; }
    switch (ticket.status) {
      case 'running': out.running += 1; break;
      case 'failed': out.failed += 1; break;
      case 'blocked':
      case 'waiting':
      case 'awaiting_clarification': out.needsYou += 1; break;
      case 'completed':
      case 'cancelled':
      case 'archived': break;
      default: out.queued += 1;
    }
  }
  return out;
}

/**
 * Tickets for one Source or schedule through the existing list endpoint. The
 * key matches useTickets({ source_id }) so the Tickets page shares the cache.
 */
function useJobCounts(filter: { source_id: string } | { schedule_id: string }) {
  return useQuery({
    queryKey: [...queryKeys.autonomy.tickets(), filter],
    queryFn: () => listAutonomyTickets(filter),
    refetchInterval: 15_000,
    select: (data) => countJobs(data.tickets ?? []),
  });
}

function JobCountCells({ filter }: { filter: { source_id: string } | { schedule_id: string } }) {
  const { t } = useTranslation();
  const counts = useJobCounts(filter);
  if (counts.isError) {
    return <div className="tn-sources-counts tn-sources-muted">{t('tnSources.counts.unavailable')}</div>;
  }
  const data = counts.data;
  return (
    <div className="tn-sources-counts" role="group" aria-label={t('tnSources.counts.label')} aria-busy={counts.isLoading}>
      {COUNT_COLUMNS.map((col) => {
        const value = data ? data[col.key] : null;
        const label = t(`tnSources.counts.${col.key}`);
        return (
          <span
            key={col.key}
            className={`tn-sources-count tn-sources-count--${col.key}${value ? '' : ' tn-sources-count--zero'}`}
            data-count={col.key}
            aria-label={value == null ? t('tnSources.counts.loading') : t('tnSources.counts.cell', { count: value, label })}
            title={label}
          >
            <span className="tn-sources-count-glyph" aria-hidden="true">{col.glyph}</span>
            <span className="tn-sources-count-num" aria-hidden="true">{value ?? '–'}</span>
            <span className="tn-sources-count-word" aria-hidden="true">{label}</span>
          </span>
        );
      })}
      <span
        className="tn-sources-count tn-sources-count--total"
        data-count="total"
        aria-label={data ? t('tnSources.counts.cell', { count: data.total, label: t('tnSources.counts.total') }) : t('tnSources.counts.loading')}
        title={t('tnSources.counts.total')}
      >
        <span className="tn-sources-count-num" aria-hidden="true">{data ? `${data.total}${data.capped ? '+' : ''}` : '–'}</span>
        <span className="tn-sources-count-word tn-sources-count-word--always" aria-hidden="true">{t('tnSources.counts.total')}</span>
      </span>
    </div>
  );
}

/** Column legend for the count cells; the cells carry their own labels. */
function CountHead() {
  const { t } = useTranslation();
  return (
    <div className="tn-sources-head" aria-hidden="true">
      <span />
      <span />
      <span className="tn-sources-counts">
        {COUNT_COLUMNS.map((col) => (
          <span key={col.key} className="tn-sources-count tn-sources-count--legend" title={t(`tnSources.counts.${col.key}`)}>
            <span className="tn-sources-count-glyph">{col.glyph}</span>
            <span className="tn-sources-count-num">{t(`tnSources.counts.${col.key}`)}</span>
          </span>
        ))}
        <span className="tn-sources-count tn-sources-count--legend tn-sources-count--total">
          <span className="tn-sources-count-num">{t('tnSources.counts.total')}</span>
        </span>
      </span>
    </div>
  );
}

// ───────────────────────── layout pieces ─────────────────────────

function Section({ id, title, lede, children }: { id: string; title: string; lede?: string; children: ReactNode }) {
  return (
    <section className="tn-sources-section" aria-labelledby={`${id}-title`}>
      <header className="tn-sources-section-head">
        <h2 id={`${id}-title`}>{title}</h2>
        {lede ? <p className="tn-sources-lede">{lede}</p> : null}
      </header>
      {children}
    </section>
  );
}

function SourceRow({
  name,
  health,
  meta,
  counts,
  actions,
  rowKey,
}: {
  name: string;
  health: SourceHealth;
  meta: ReactNode;
  counts: ReactNode;
  actions: ReactNode;
  rowKey: string;
}) {
  const { t } = useTranslation();
  return (
    <li className={`tn-sources-row tn-sources-row--${health}`} data-row={rowKey}>
      <span className="tn-sources-cell-health"><HealthMark health={health} /></span>
      <div className="tn-sources-cell-main">
        <strong className="tn-sources-name">{name}</strong>
        {meta}
      </div>
      <div className="tn-sources-cell-counts">{counts}</div>
      <div className="tn-sources-actions" role="group" aria-label={t('tnSources.row.actions', { name })}>
        {actions}
      </div>
    </li>
  );
}

// ───────────────────────── Router health ─────────────────────────

function RouterHealthPanel() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const evals = useQuery({
    queryKey: [...queryKeys.autonomy.all, 'unavailable-evals'],
    queryFn: getUnavailableEvals,
    refetchInterval: 15_000,
  });
  const requeue = useMutation({
    mutationFn: requeueEval,
    onSuccess: () => void qc.invalidateQueries({ queryKey: [...queryKeys.autonomy.all, 'unavailable-evals'] }),
  });
  const rows = evals.data?.evals ?? [];
  if (!rows.length) return null;
  return (
    <Section id="tn-sources-router" title={t('tnSources.router.title')} lede={t('tnSources.router.lede')}>
      <ul className="tn-sources-list">
        {rows.map((row) => (
          <SourceRow
            key={row.eval_id}
            rowKey={row.eval_id}
            name={row.thread_key}
            health="error"
            meta={(
              <p className="tn-sources-meta">
                {row.reason || t('tnSources.router.fallbackReason')} · {t('tnSources.router.triggers', { count: row.trigger_ids.length })}
              </p>
            )}
            counts={null}
            actions={(
              <button type="button" className="tn-sources-btn" disabled={requeue.isPending} onClick={() => requeue.mutate(row.eval_id)}>
                {t('tnSources.router.requeue')}
              </button>
            )}
          />
        ))}
      </ul>
      {requeue.isError ? <p className="sa-error">{t('tnSources.router.requeueFailed')}</p> : null}
    </Section>
  );
}

// ───────────────────────── Policy ─────────────────────────

function PolicyPanel() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const policy = useQuery({ queryKey: [...queryKeys.autonomy.all, 'policy'], queryFn: getAutonomyPolicy });
  const update = useMutation({
    mutationFn: updateAutonomyPolicy,
    onSuccess: () => void qc.invalidateQueries({ queryKey: [...queryKeys.autonomy.all, 'policy'] }),
  });
  const admission = policy.data?.admission;
  const busy = policy.isLoading || update.isPending;
  return (
    <Section id="tn-sources-policy" title={t('tnSources.policy.title')} lede={t('tnSources.policy.lede')}>
      <div className="tn-sources-form">
        <label>{t('tnSources.policy.acceptMode')}
          <select
            value={policy.data?.accept_mode ?? 'explicit'}
            disabled={busy}
            onChange={(e) => update.mutate({ accept_mode: e.target.value as 'explicit' | 'observe' | 'auto' })}
          >
            <option value="explicit">{t('tnSources.policy.explicitOnly')}</option>
            <option value="observe">{t('tnSources.policy.observe')}</option>
            <option value="auto">{t('tnSources.policy.auto')}</option>
          </select>
        </label>
      </div>
      <h3 className="tn-sources-subhead">{t('tnSources.policy.admissionTitle')}</h3>
      <p className="tn-sources-lede">{t('tnSources.policy.admissionLede')}</p>
      <div className="tn-sources-form">
        <label>{t('tnSources.admission.listedUnaddressed')}
          <select
            value={admission?.listed_unaddressed ?? 'inferred'}
            disabled={busy}
            onChange={(e) => update.mutate({ admission: { listed_unaddressed: e.target.value as 'inferred' | 'ignore' } })}
          >
            <option value="inferred">{t('tnSources.admission.inferred')}</option>
            <option value="ignore">{t('tnSources.admission.ignore')}</option>
          </select>
        </label>
        <label>{t('tnSources.admission.unlistedAddressed')}
          <select
            value={admission?.unlisted_addressed ?? 'ignore'}
            disabled={busy}
            onChange={(e) => update.mutate({ admission: { unlisted_addressed: e.target.value as 'explicit' | 'inferred' | 'ignore' } })}
          >
            <option value="ignore">{t('tnSources.admission.ignore')}</option>
            <option value="inferred">{t('tnSources.admission.inferred')}</option>
            <option value="explicit">{t('tnSources.admission.explicit')}</option>
          </select>
        </label>
        <label>{t('tnSources.admission.unlistedUnaddressed')}
          <select
            value={admission?.unlisted_unaddressed ?? 'ignore'}
            disabled={busy}
            onChange={(e) => update.mutate({ admission: { unlisted_unaddressed: e.target.value as 'inferred' | 'ignore' } })}
          >
            <option value="ignore">{t('tnSources.admission.ignore')}</option>
            <option value="inferred">{t('tnSources.admission.inferred')}</option>
          </select>
        </label>
        <label>{t('tnSources.admission.bots')}
          <select
            value={admission?.bot ?? 'ignore'}
            disabled={busy}
            onChange={(e) => update.mutate({ admission: { bot: e.target.value as 'inferred' | 'ignore' } })}
          >
            <option value="ignore">{t('tnSources.admission.ignore')}</option>
            <option value="inferred">{t('tnSources.admission.inferred')}</option>
          </select>
        </label>
      </div>
      {update.isError ? <p className="sa-error">{t('tnSources.policy.updateFailed')}</p> : null}
    </Section>
  );
}

// ───────────────────────── shared worker fields ─────────────────────────

type DaemonRow = { daemon_id: string; name?: string | null; provider: string };

function daemonName(daemonRows: DaemonRow[], id: string): string {
  return daemonRows.find((daemon) => daemon.daemon_id === id)?.name || id.slice(0, 8);
}

/** "Run Tickets on": server, or a local daemon compatible with the provider. */
function RunOnSelect({
  daemonId,
  provider,
  daemonRows,
  onChange,
}: {
  daemonId: string;
  provider: string;
  daemonRows: DaemonRow[];
  onChange: (value: string) => void;
}) {
  const { t } = useTranslation();
  const compatible = daemonRows.filter((daemon) => !provider || daemon.provider === provider);
  return (
    <label>{t('tnSources.worker.runOn')}
      <select value={daemonId} onChange={(e) => onChange(e.target.value)}>
        <option value="">{t('tnSources.worker.server')}</option>
        {daemonId && !compatible.some((daemon) => daemon.daemon_id === daemonId) ? (
          <option value={daemonId} disabled>
            {daemonRows.some((daemon) => daemon.daemon_id === daemonId)
              ? t('tnSources.worker.providerMismatch', { name: daemonName(daemonRows, daemonId) })
              : t('tnSources.worker.unavailableDaemon', { id: daemonId.slice(0, 8) })}
          </option>
        ) : null}
        {compatible.map((daemon) => (
          <option key={daemon.daemon_id} value={daemon.daemon_id}>
            {t('tnSources.worker.localOption', { name: daemon.name || daemon.daemon_id.slice(0, 8), provider: daemon.provider })}
          </option>
        ))}
      </select>
    </label>
  );
}

function ProviderSelect({
  label,
  provider,
  daemonId,
  native,
  onChange,
}: {
  label: string;
  provider: string;
  daemonId: string;
  native: { provider: string }[];
  onChange: (value: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <label>{label}
      <select value={provider} onChange={(e) => onChange(e.target.value)}>
        <option value="">{daemonId ? t('tnSources.worker.useDaemonProvider') : t('tnSources.worker.ptcAgent')}</option>
        {provider && !native.some((runtime) => runtime.provider === provider) ? (
          <option value={provider}>{t('tnSources.worker.saved', { label: nativeRuntimeLabel(provider) })}</option>
        ) : null}
        {native.map((r) => <option key={r.provider} value={r.provider}>{nativeRuntimeLabel(r.provider)}</option>)}
      </select>
    </label>
  );
}

function WorkspaceSelect({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const { t } = useTranslation();
  const workspaces = useAllWorkspaces({ includeHome: true });
  return (
    <label>{t('tnSources.worker.workspace')}
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">{t('tnSources.worker.choose')}</option>
        {(workspaces.data?.workspaces ?? []).map((ws) => <option key={ws.workspace_id} value={ws.workspace_id}>{ws.name}</option>)}
      </select>
    </label>
  );
}

// ───────────────────────── Sources (GitHub / Feishu) ─────────────────────────

type CheckResult = { source_id: string; ok: boolean; error?: string; agent_login?: string; can_push?: boolean };

const EMPTY_ADMISSION = { listed_unaddressed: '', unlisted_addressed: '', unlisted_unaddressed: '' };

/** Polled Sources: dense rows (health, job counts, actions) plus the add/edit form. */
export function SourcesPanel() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const sources = useQuery({ queryKey: queryKeys.autonomy.sources(), queryFn: listAutonomySources, refetchInterval: 10_000 });
  const { data: runtimes } = useNativeAgents(true);
  const daemons = useQuery({ queryKey: queryKeys.autonomy.daemons(), queryFn: listAutonomyDaemons, staleTime: 15_000 });
  const native = (runtimes ?? []).filter((r) => r.installed && r.live_verified);
  const [kind, setKind] = useState<'github' | 'feishu'>('github');
  const [repository, setRepository] = useState('');
  const [secretName, setSecretName] = useState('GITHUB_TOKEN');
  const [workspaceId, setWorkspaceId] = useState('');
  const [allowed, setAllowed] = useState('');
  const [bots, setBots] = useState('');
  const [pollSeconds, setPollSeconds] = useState(60);
  const [provider, setProvider] = useState('');
  const [daemonId, setDaemonId] = useState('');
  const [model, setModel] = useState('');
  const [chatId, setChatId] = useState('');
  const [admission, setAdmission] = useState(EMPTY_ADMISSION);
  const [agentOpenId, setAgentOpenId] = useState('');
  const [apiBaseUrl, setApiBaseUrl] = useState('');
  const [polledId, setPolledId] = useState<string | null>(null);
  const [checkResult, setCheckResult] = useState<CheckResult | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const splitList = (value: string) => value.split(',').map((v) => v.trim()).filter(Boolean);
  const admissionInput = Object.fromEntries(Object.entries(admission).filter(([, v]) => v));
  const sourceInput = kind === 'github'
    ? {
        repository: repository.trim(),
        secret_name: secretName.trim(),
        workspace_id: workspaceId,
        allowed_logins: splitList(allowed),
        bot_logins: splitList(bots),
        poll_seconds: pollSeconds,
        worker_provider: provider,
        worker_daemon_id: daemonId || null,
        worker_model: model.trim(),
        admission: admissionInput,
        api_base_url: apiBaseUrl.trim(),
      }
    : {
        chat_id: chatId.trim(),
        workspace_id: workspaceId,
        allowed_logins: splitList(allowed),
        bot_logins: splitList(bots),
        poll_seconds: pollSeconds,
        worker_provider: provider,
        worker_daemon_id: daemonId || null,
        worker_model: model.trim(),
        agent_open_id: agentOpenId.trim(),
        admission: admissionInput,
      };
  const resetForm = () => {
    setEditingId(null); setKind('github');
    setRepository(''); setSecretName('GITHUB_TOKEN'); setWorkspaceId('');
    setAllowed(''); setBots(''); setPollSeconds(60); setProvider(''); setDaemonId(''); setModel('');
    setChatId(''); setAgentOpenId(''); setAdmission(EMPTY_ADMISSION); setApiBaseUrl(''); setError(null);
  };
  const startEdit = (source: AutonomySource) => {
    setEditingId(source.source_id);
    setKind(source.kind === 'feishu' ? 'feishu' : 'github');
    setRepository(source.config.repository ?? '');
    setSecretName(source.config.secret_name ?? 'GITHUB_TOKEN');
    setWorkspaceId(source.config.workspace_id);
    setAllowed((source.config.allowed_logins ?? []).join(', '));
    setBots((source.config.bot_logins ?? []).join(', '));
    setPollSeconds(source.config.poll_seconds);
    setProvider(source.config.worker_provider ?? '');
    setDaemonId(source.config.worker_daemon_id ?? '');
    setModel(source.config.worker_model ?? '');
    setChatId(source.config.chat_id ?? '');
    setAgentOpenId(source.config.agent_open_id ?? '');
    setAdmission({
      listed_unaddressed: source.config.admission?.listed_unaddressed ?? '',
      unlisted_addressed: source.config.admission?.unlisted_addressed ?? '',
      unlisted_unaddressed: source.config.admission?.unlisted_unaddressed ?? '',
    });
    setApiBaseUrl(source.config.api_base_url ?? '');
    setError(null);
  };
  const check = useMutation({
    mutationFn: (id: string) => checkAutonomySource(id),
    onSuccess: (result, id) => {
      setCheckResult({ source_id: id, ...result });
      void qc.invalidateQueries({ queryKey: queryKeys.autonomy.all });
    },
  });
  const create = useMutation({
    mutationFn: () => createAutonomySource({ kind, config: sourceInput, enabled: true }),
    onSuccess: (source) => {
      resetForm();
      void qc.invalidateQueries({ queryKey: queryKeys.autonomy.all });
      void check.mutateAsync(source.source_id).catch(() => undefined);
    },
    onError: (err: Error) => setError(err.message),
  });
  const update = useMutation({
    mutationFn: () => updateAutonomySource(editingId!, { config: sourceInput }),
    onSuccess: () => {
      resetForm();
      void qc.invalidateQueries({ queryKey: queryKeys.autonomy.all });
    },
    onError: (err: Error) => setError(err.message),
  });
  const poll = useMutation({
    mutationFn: (id: string) => pollAutonomySource(id),
    onSuccess: (source) => {
      setPolledId(source.source_id);
      void qc.invalidateQueries({ queryKey: queryKeys.autonomy.all });
    },
  });
  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => updateAutonomySource(id, { enabled }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: queryKeys.autonomy.all }),
  });
  const remove = useMutation({
    mutationFn: (id: string) => deleteAutonomySource(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: queryKeys.autonomy.all }),
  });
  const polledSources = (sources.data?.sources ?? []).filter((s: AutonomySource) => ['github', 'feishu'].includes(s.kind));
  const daemonRows: DaemonRow[] = daemons.data?.daemons ?? [];
  const kindLabel = kind === 'github' ? t('tnSources.sources.github') : t('tnSources.sources.feishu');
  return (
    <Section id="tn-sources-sources" title={t('tnSources.sources.title')} lede={t('tnSources.sources.lede')}>
      {sources.isError ? <p className="sa-error">{t('tnSources.sources.readFailed')}</p> : null}
      {polledSources.length ? (
        <>
          <CountHead />
          <ul className="tn-sources-list">
            {polledSources.map((source) => {
              const name = source.config.repository || source.config.chat_id || source.source_id;
              const checked = checkResult?.source_id === source.source_id ? checkResult : null;
              return (
                <SourceRow
                  key={source.source_id}
                  rowKey={source.source_id}
                  name={name}
                  health={sourceHealth(source)}
                  meta={(
                    <>
                      <p className="tn-sources-meta">
                        {source.kind === 'feishu' ? t('tnSources.sources.feishu') : source.config.secret_name}
                        {' · '}{t('tnSources.sources.every', { seconds: source.config.poll_seconds })}
                        {' · '}{source.state.agent_login || t('tnSources.sources.notChecked')}
                        {' · '}{source.config.worker_daemon_id
                          ? t('tnSources.worker.localDaemon', { name: daemonName(daemonRows, source.config.worker_daemon_id) })
                          : t('tnSources.worker.serverExecution')}
                      </p>
                      <p className="tn-sources-meta">
                        {source.state.last_poll_at
                          ? t('tnSources.sources.lastPoll', { time: new Date(source.state.last_poll_at).toLocaleString() })
                          : t('tnSources.sources.notPolled')}
                        {source.state.cursor ? ` · ${t('tnSources.sources.cursor', { cursor: source.state.cursor.slice(0, 8) })}` : ''}
                      </p>
                      {source.state.last_error ? <p className="tn-sources-error-line">{source.state.last_error}</p> : null}
                      {checked ? (
                        checked.ok ? (
                          <p className="tn-sources-meta">
                            {t('tnSources.sources.checkedAs', {
                              login: checked.agent_login || t('tnSources.sources.unknownLogin'),
                              access: checked.can_push ? t('tnSources.sources.canPush') : t('tnSources.sources.readOnly'),
                            })}
                          </p>
                        ) : (
                          <p className="tn-sources-error-line">{checked.error || t('tnSources.sources.checkFailed')}</p>
                        )
                      ) : null}
                    </>
                  )}
                  counts={<JobCountCells filter={{ source_id: source.source_id }} />}
                  actions={(
                    <>
                      <button type="button" className="tn-sources-btn" onClick={() => startEdit(source)}>{t('tnSources.sources.edit')}</button>
                      <button type="button" className="tn-sources-btn" onClick={() => navigate(`/tasks?source=${encodeURIComponent(source.source_id)}`)}>{t('tnSources.sources.tickets')}</button>
                      <button type="button" className="tn-sources-btn" onClick={() => check.mutate(source.source_id)}>{t('tnSources.sources.check')}</button>
                      <button type="button" className="tn-sources-btn" disabled={!source.enabled || poll.isPending} onClick={() => poll.mutate(source.source_id)}>
                        {polledId === source.source_id ? t('tnSources.sources.pollQueued') : t('tnSources.sources.pollNow')}
                      </button>
                      <button type="button" className="tn-sources-btn" onClick={() => toggle.mutate({ id: source.source_id, enabled: !source.enabled })}>
                        {source.enabled ? t('tnSources.sources.disable') : t('tnSources.sources.enable')}
                      </button>
                      <button
                        type="button"
                        className="tn-sources-btn tn-sources-btn--danger"
                        onClick={() => {
                          if (window.confirm(t('tnSources.sources.confirmDelete', { name }))) {
                            remove.mutate(source.source_id);
                          }
                        }}
                      >
                        {t('tnSources.sources.delete')}
                      </button>
                    </>
                  )}
                />
              );
            })}
          </ul>
        </>
      ) : sources.isSuccess ? <p className="tn-sources-muted tn-sources-empty">{t('tnSources.sources.empty')}</p> : null}
      <h3 className="tn-sources-subhead">
        {editingId ? t('tnSources.sources.editTitle', { kind: kindLabel }) : t('tnSources.sources.addTitle', { kind: kindLabel })}
      </h3>
      <p className="tn-sources-lede">{t('tnSources.sources.vaultNote')}</p>
      <div className="tn-sources-form">
        <label>{t('tnSources.sources.kind')}
          <select value={kind} onChange={(e) => setKind(e.target.value as 'github' | 'feishu')}>
            <option value="github">{t('tnSources.sources.github')}</option>
            <option value="feishu">{t('tnSources.sources.feishu')}</option>
          </select>
        </label>
        {kind === 'github' ? (
          <>
            <label>{t('tnSources.sources.repository')}<input value={repository} onChange={(e) => setRepository(e.target.value)} placeholder="owner/repo" /></label>
            <label>{t('tnSources.sources.vaultSecret')}<input value={secretName} onChange={(e) => setSecretName(e.target.value)} /></label>
          </>
        ) : (
          <>
            <label>{t('tnSources.sources.chatId')}<input value={chatId} onChange={(e) => setChatId(e.target.value)} placeholder="oc_..." /></label>
            <label>{t('tnSources.sources.agentOpenId')}<input value={agentOpenId} onChange={(e) => setAgentOpenId(e.target.value)} placeholder={t('tnSources.sources.agentOpenIdPlaceholder')} /></label>
          </>
        )}
        <WorkspaceSelect value={workspaceId} onChange={setWorkspaceId} />
        <label>{kind === 'feishu' ? t('tnSources.sources.allowedFeishu') : t('tnSources.sources.allowedGithub')}<input value={allowed} onChange={(e) => setAllowed(e.target.value)} placeholder="alice, bob" /></label>
        <label>{t('tnSources.sources.allowedBots')}<input value={bots} onChange={(e) => setBots(e.target.value)} placeholder="dependabot[bot], ..." /></label>
        <ProviderSelect label={t('tnSources.sources.workerAgent')} provider={provider} daemonId={daemonId} native={native} onChange={setProvider} />
        <label>{t('tnSources.sources.workerModel')}<input value={model} onChange={(e) => setModel(e.target.value)} placeholder={t('tnSources.sources.workerModelPlaceholder')} /></label>
        <RunOnSelect daemonId={daemonId} provider={provider} daemonRows={daemonRows} onChange={setDaemonId} />
        <label>{t('tnSources.sources.pollSeconds')}<input type="number" min={15} max={3600} value={pollSeconds} onChange={(e) => setPollSeconds(Number(e.target.value))} /></label>
        {kind === 'github' ? (
          <label>{t('tnSources.sources.apiBaseUrl')}<input value={apiBaseUrl} onChange={(e) => setApiBaseUrl(e.target.value)} placeholder={t('tnSources.sources.apiBaseUrlPlaceholder')} /></label>
        ) : null}
      </div>
      {daemonId && !provider ? <p className="tn-sources-muted">{t('tnSources.worker.daemonProvides')}</p> : null}
      <details className="tn-sources-details">
        <summary>{t('tnSources.admission.overrides')}</summary>
        <p className="tn-sources-lede">{t('tnSources.admission.overridesLede')}</p>
        <div className="tn-sources-form">
          <label>{t('tnSources.admission.listedUnaddressed')}
            <select value={admission.listed_unaddressed} onChange={(e) => setAdmission({ ...admission, listed_unaddressed: e.target.value })}>
              <option value="">{t('tnSources.admission.usePolicy')}</option>
              <option value="inferred">{t('tnSources.admission.inferred')}</option>
              <option value="ignore">{t('tnSources.admission.ignore')}</option>
            </select>
          </label>
          <label>{t('tnSources.admission.unlistedAddressed')}
            <select value={admission.unlisted_addressed} onChange={(e) => setAdmission({ ...admission, unlisted_addressed: e.target.value })}>
              <option value="">{t('tnSources.admission.usePolicy')}</option>
              <option value="explicit">{t('tnSources.admission.explicit')}</option>
              <option value="inferred">{t('tnSources.admission.inferred')}</option>
              <option value="ignore">{t('tnSources.admission.ignore')}</option>
            </select>
          </label>
          <label>{t('tnSources.admission.unlistedUnaddressed')}
            <select value={admission.unlisted_unaddressed} onChange={(e) => setAdmission({ ...admission, unlisted_unaddressed: e.target.value })}>
              <option value="">{t('tnSources.admission.usePolicy')}</option>
              <option value="inferred">{t('tnSources.admission.inferred')}</option>
              <option value="ignore">{t('tnSources.admission.ignore')}</option>
            </select>
          </label>
        </div>
      </details>
      <div className="tn-sources-submit">
        <button
          type="button"
          className="sa-primary"
          disabled={create.isPending || update.isPending || (kind === 'github' ? !repository.trim() : !chatId.trim()) || !workspaceId}
          onClick={() => {
            if (!editingId) { create.mutate(); return; }
            update.mutate();
          }}
        >
          {editingId ? t('tnSources.worker.save') : t('tnSources.sources.add')}
        </button>
        {editingId ? <button type="button" className="tn-sources-btn" onClick={resetForm}>{t('tnSources.worker.cancel')}</button> : null}
      </div>
      {error ? <div className="sa-error">{error}</div> : null}
    </Section>
  );
}

// ───────────────────────── Schedules ─────────────────────────

export function SchedulePanel() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { data: runtimes } = useNativeAgents(true);
  const daemons = useQuery({ queryKey: queryKeys.autonomy.daemons(), queryFn: listAutonomyDaemons, staleTime: 15_000 });
  const schedules = useQuery({ queryKey: [...queryKeys.autonomy.sources(), 'schedules'], queryFn: listAutonomySchedules, refetchInterval: 30_000 });
  const [prompt, setPrompt] = useState('');
  const [title, setTitle] = useState('');
  const [cron, setCron] = useState('0 9 * * 1-5');
  const [zone, setZone] = useState(timezone());
  const [workspaceId, setWorkspaceId] = useState('');
  const [provider, setProvider] = useState('');
  const [daemonId, setDaemonId] = useState('');
  const [editingId, setEditingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const native = (runtimes ?? []).filter((r) => r.installed && r.live_verified);
  const input = {
    prompt: prompt.trim(), title: title.trim(), workspace_id: workspaceId,
    cron_expression: cron.trim(), timezone: zone, worker_provider: provider, worker_daemon_id: daemonId || null,
  };
  const reset = () => {
    setEditingId(null);
    setPrompt(''); setTitle(''); setCron('0 9 * * 1-5'); setZone(timezone());
    setWorkspaceId(''); setProvider(''); setDaemonId(''); setError(null);
  };
  const create = useMutation({
    mutationFn: () => createAutonomySchedule(input),
    onSuccess: () => {
      reset();
      void qc.invalidateQueries({ queryKey: queryKeys.autonomy.all });
    },
    onError: (err: Error) => setError(err.message),
  });
  const update = useMutation({
    mutationFn: () => updateAutonomySchedule(editingId!, input),
    onSuccess: () => {
      reset();
      void qc.invalidateQueries({ queryKey: queryKeys.autonomy.all });
    },
    onError: (err: Error) => setError(err.message),
  });
  const startEdit = (row: AutonomySchedule) => {
    setEditingId(row.schedule_id);
    setPrompt(row.prompt); setTitle(row.title); setCron(row.cron_expression);
    setZone(row.timezone); setWorkspaceId(row.workspace_id);
    setProvider(row.worker_provider ?? ''); setDaemonId(row.worker_daemon_id ?? ''); setError(null);
  };
  const startClone = (row: AutonomySchedule) => {
    setEditingId(null);
    setPrompt(row.prompt);
    setTitle(t('tnSources.schedules.copySuffix', { title: row.title || row.cron_expression }));
    setCron(row.cron_expression); setZone(row.timezone);
    setWorkspaceId(row.workspace_id); setProvider(row.worker_provider ?? '');
    setDaemonId(row.worker_daemon_id ?? '');
    setError(null);
  };
  const remove = useMutation({
    mutationFn: deleteAutonomySchedule,
    onSuccess: () => void qc.invalidateQueries({ queryKey: queryKeys.autonomy.all }),
  });
  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => updateAutonomySchedule(id, { enabled }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: queryKeys.autonomy.all }),
  });
  const runNow = useMutation({
    mutationFn: runAutonomySchedule,
    onSuccess: (ticket) => {
      void qc.invalidateQueries({ queryKey: queryKeys.autonomy.all });
      navigate(`/tasks/${ticket.ticket_id}`);
    },
  });
  const rows: AutonomySchedule[] = schedules.data?.schedules ?? [];
  const daemonRows: DaemonRow[] = daemons.data?.daemons ?? [];
  return (
    <Section id="tn-sources-schedules" title={t('tnSources.schedules.title')} lede={t('tnSources.schedules.lede')}>
      {schedules.isError ? <p className="sa-error">{t('tnSources.schedules.readFailed')}</p> : null}
      {rows.length ? (
        <>
          <CountHead />
          <ul className="tn-sources-list">
            {rows.map((row) => (
              <SourceRow
                key={row.schedule_id}
                rowKey={row.schedule_id}
                name={row.title || row.cron_expression}
                health={scheduleHealth(row)}
                meta={(
                  <>
                    <p className="tn-sources-meta">
                      <code className="tn-sources-code">{row.cron_expression}</code>
                      {' · '}{row.timezone}
                      {' · '}{t('tnSources.schedules.next', { time: new Date(row.next_run_at).toLocaleString() })}
                      {' · '}{row.last_run_at
                        ? t('tnSources.schedules.last', { time: new Date(row.last_run_at).toLocaleString() })
                        : t('tnSources.schedules.neverRun')}
                    </p>
                    <p className="tn-sources-meta">
                      {row.worker_daemon_id
                        ? t('tnSources.worker.localDaemon', { name: daemonName(daemonRows, row.worker_daemon_id) })
                        : t('tnSources.worker.serverExecution')}
                    </p>
                    {row.last_error ? <p className="tn-sources-error-line">{row.last_error}</p> : null}
                  </>
                )}
                counts={<JobCountCells filter={{ schedule_id: row.schedule_id }} />}
                actions={(
                  <>
                    <button type="button" className="tn-sources-btn" onClick={() => navigate(`/tasks?schedule=${encodeURIComponent(row.schedule_id)}`)}>{t('tnSources.sources.tickets')}</button>
                    <button type="button" className="tn-sources-btn" onClick={() => startClone(row)}>{t('tnSources.schedules.duplicate')}</button>
                    <button type="button" className="tn-sources-btn" onClick={() => startEdit(row)}>{t('tnSources.sources.edit')}</button>
                    <button type="button" className="tn-sources-btn" onClick={() => runNow.mutate(row.schedule_id)}>{t('tnSources.schedules.runNow')}</button>
                    <button type="button" className="tn-sources-btn" onClick={() => toggle.mutate({ id: row.schedule_id, enabled: !row.enabled })}>
                      {row.enabled ? t('tnSources.schedules.pause') : t('tnSources.schedules.resume')}
                    </button>
                    <button type="button" className="tn-sources-btn tn-sources-btn--danger" onClick={() => remove.mutate(row.schedule_id)}>{t('tnSources.sources.delete')}</button>
                  </>
                )}
              />
            ))}
          </ul>
        </>
      ) : schedules.isSuccess ? <p className="tn-sources-muted tn-sources-empty">{t('tnSources.schedules.empty')}</p> : null}
      <h3 className="tn-sources-subhead">{editingId ? t('tnSources.schedules.editTitle') : t('tnSources.schedules.newTitle')}</h3>
      <div className="tn-sources-form">
        <label className="tn-sources-wide">{t('tnSources.schedules.prompt')}<textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={3} /></label>
        <label>{t('tnSources.schedules.titleField')}<input value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t('tnSources.schedules.titlePlaceholder')} /></label>
        <label>{t('tnSources.schedules.cron')}<input value={cron} onChange={(e) => setCron(e.target.value)} placeholder="0 9 * * 1-5" /></label>
        <label>{t('tnSources.schedules.timezone')}<input value={zone} onChange={(e) => setZone(e.target.value)} /></label>
        <WorkspaceSelect value={workspaceId} onChange={setWorkspaceId} />
        <ProviderSelect label={t('tnSources.schedules.agent')} provider={provider} daemonId={daemonId} native={native} onChange={setProvider} />
        <RunOnSelect daemonId={daemonId} provider={provider} daemonRows={daemonRows} onChange={setDaemonId} />
      </div>
      {daemonId && !provider ? <p className="tn-sources-muted">{t('tnSources.worker.daemonProvides')}</p> : null}
      <div className="tn-sources-submit">
        <button
          type="button"
          className="sa-primary"
          disabled={create.isPending || update.isPending || !prompt.trim() || !workspaceId}
          onClick={() => (editingId ? update.mutate() : create.mutate())}
        >
          {editingId ? t('tnSources.worker.save') : t('tnSources.schedules.create')}
        </button>
        {editingId ? <button type="button" className="tn-sources-btn" onClick={reset}>{t('tnSources.worker.cancel')}</button> : null}
      </div>
      {error ? <div className="sa-error">{error}</div> : null}
    </Section>
  );
}

export default function SourcesPage() {
  const { t } = useTranslation();
  return (
    <div className="sa-page tn-sources">
      <header className="sa-page-head">
        <div>
          <h1>{t('tnSources.page.title')}</h1>
          <p>{t('tnSources.page.lede')}</p>
        </div>
      </header>
      <RouterHealthPanel />
      <SourcesPanel />
      <SchedulePanel />
      <PolicyPanel />
    </div>
  );
}
