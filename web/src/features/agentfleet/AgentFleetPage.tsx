import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { getAgentFleet, getAgentLogs, type FleetAgent } from '@/api/agentFleet';
import { apiErrorStatus } from '@/api/core/api/errors';
import { queryKeys } from '@/lib/queryKeys';
import {
  AGENT_ROLES,
  AGENT_STATUSES,
  BALANCE_MARKS,
  EMPTY_FILTERS,
  SORT_KEYS,
  STATUS_MARKS,
  filterAgents,
  formatElapsed,
  providerRoster,
  refreshIntervalFromQuery,
  sortAgents,
  summarizeTail,
  type FleetFilters,
  type Mark,
  type SortKey,
} from './fleetView';
import './fleetLocales';
import './agentFleet.css';

/** A glyph and a word: a state is never told by colour alone. */
function MarkTag({
  mark,
  word,
  title,
  spin = false,
  kind,
}: {
  mark: Mark;
  word: string;
  title?: string;
  spin?: boolean;
  kind: string;
}) {
  return (
    <span className={`tn-fleet-mark tn-fleet-mark--${mark.ink} ${kind}`} title={title}>
      <span
        className={`tn-fleet-glyph${spin ? ' tn-fleet-glyph--spin' : ''}`}
        aria-hidden="true"
      >
        {mark.glyph}
      </span>
      <span className="tn-fleet-word">{word}</span>
    </span>
  );
}

function StatusMark({ status }: { status: FleetAgent['status'] }) {
  const { t } = useTranslation();
  return (
    <MarkTag
      kind={`tn-fleet-status tn-fleet-status--${status}`}
      mark={STATUS_MARKS[status]}
      word={t(`tnAgentFleet.status.${status}`)}
      spin={status === 'running'}
    />
  );
}

function BalanceMark({ state }: { state: FleetAgent['provider_balance'] }) {
  const { t } = useTranslation();
  return (
    <MarkTag
      kind={`tn-fleet-balance tn-fleet-balance--${state}`}
      mark={BALANCE_MARKS[state]}
      word={t(`tnAgentFleet.balance.${state}`)}
      title={t(`tnAgentFleet.balanceTitle.${state}`)}
    />
  );
}

function AgentLogsPanel({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const logs = useQuery({
    queryKey: queryKeys.agentFleet.logs(agentId),
    queryFn: () => getAgentLogs(agentId, 200),
    staleTime: 10_000,
  });
  if (logs.isPending) return <p className="tn-fleet-note">{t('tnAgentFleet.logs.loading')}</p>;
  if (logs.isError) {
    return (
      <p className="tn-fleet-note tn-fleet-note--bad">
        {apiErrorStatus(logs.error) === 404
          ? t('tnAgentFleet.logs.missing')
          : t('tnAgentFleet.logs.failed')}
      </p>
    );
  }
  return (
    <div className="tn-fleet-logs">
      <div className="tn-fleet-logs-head">
        <span className="tn-fleet-mono">{logs.data.log_file}</span>
        {logs.data.truncated && (
          <span className="tn-fleet-note">{t('tnAgentFleet.logs.truncated')}</span>
        )}
      </div>
      {/* Server-redacted text only; the panel never shows a raw log line. */}
      <pre>{logs.data.lines.join('\n')}</pre>
    </div>
  );
}

export default function AgentFleetPage() {
  const { t, i18n } = useTranslation();
  const [searchParams] = useSearchParams();
  const [filters, setFilters] = useState<FleetFilters>(EMPTY_FILTERS);
  const [sortKey, setSortKey] = useState<SortKey>('status');
  const [openLogs, setOpenLogs] = useState<string | null>(null);

  const interval = useMemo(() => refreshIntervalFromQuery(searchParams.toString()), [searchParams]);
  const fleet = useQuery({
    queryKey: queryKeys.agentFleet.fleet(),
    queryFn: getAgentFleet,
    refetchInterval: interval,
    staleTime: 5_000,
  });

  const allAgents = useMemo(() => fleet.data?.agents ?? [], [fleet.data]);
  const visible = useMemo(
    () => sortAgents(filterAgents(allAgents, filters), sortKey),
    [allAgents, filters, sortKey],
  );
  const roster = useMemo(() => providerRoster(allAgents), [allAgents]);
  // A 403 is the loopback gate explaining itself; any other failure (500, a
  // dropped connection) says nothing about the fleet, so it must not be drawn
  // as an empty one — the table and the "no agents" note both imply a scan that
  // answered, which is exactly the wrong reading of a request that never did.
  // The backend reports a failed or disabled scan the same way, as a 200 whose
  // body carries `error`, so that answer is a failure too.
  const forbidden = fleet.isError && apiErrorStatus(fleet.error) === 403;
  const failed = (fleet.isError && !forbidden) || Boolean(fleet.data?.error);
  const answered = !fleet.isPending && !forbidden && !failed;

  const col = (key: string) => t(`tnAgentFleet.column.${key}`);

  return (
    <div className="tn-fleet">
      <header className="tn-fleet-head">
        <h1>{t('tnAgentFleet.title')}</h1>
        <p className="tn-fleet-sub">{t('tnAgentFleet.subtitle')}</p>
      </header>

      <div
        className="tn-fleet-filters"
        role="group"
        aria-label={t('tnAgentFleet.filter.label')}
      >
        <label>
          <span>{t('tnAgentFleet.filter.status')}</span>
          <select
            value={filters.status}
            onChange={(event) =>
              setFilters({ ...filters, status: event.target.value as FleetFilters['status'] })
            }
          >
            <option value="all">{t('tnAgentFleet.filter.all')}</option>
            {AGENT_STATUSES.map((status) => (
              <option key={status} value={status}>
                {t(`tnAgentFleet.status.${status}`)}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>{t('tnAgentFleet.filter.role')}</span>
          <select
            value={filters.role}
            onChange={(event) =>
              setFilters({ ...filters, role: event.target.value as FleetFilters['role'] })
            }
          >
            <option value="all">{t('tnAgentFleet.filter.all')}</option>
            {AGENT_ROLES.map((role) => (
              <option key={role} value={role}>
                {t(`tnAgentFleet.role.${role}`)}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>{t('tnAgentFleet.filter.provider')}</span>
          <input
            value={filters.provider}
            onChange={(event) => setFilters({ ...filters, provider: event.target.value })}
            placeholder={t('tnAgentFleet.filter.substring')}
            spellCheck={false}
            autoComplete="off"
          />
        </label>
        <label>
          <span>{t('tnAgentFleet.filter.model')}</span>
          <input
            value={filters.model}
            onChange={(event) => setFilters({ ...filters, model: event.target.value })}
            placeholder={t('tnAgentFleet.filter.substring')}
            spellCheck={false}
            autoComplete="off"
          />
        </label>
        <label>
          <span>{t('tnAgentFleet.filter.sort')}</span>
          <select value={sortKey} onChange={(event) => setSortKey(event.target.value as SortKey)}>
            {SORT_KEYS.map((key) => (
              <option key={key} value={key}>
                {t(`tnAgentFleet.sort.${key}`)}
              </option>
            ))}
          </select>
        </label>
      </div>

      {forbidden && (
        <p className="tn-fleet-note tn-fleet-note--bad" role="alert">
          {t('tnAgentFleet.loopbackOnly')}
        </p>
      )}
      {failed && (
        <p className="tn-fleet-note tn-fleet-note--bad" role="alert">
          {t('tnAgentFleet.loadFailed')}
        </p>
      )}
      {fleet.data?.error && (
        <p className="tn-fleet-note tn-fleet-note--bad">{fleet.data.error}</p>
      )}
      {fleet.isPending && <p className="tn-fleet-note">{t('tnAgentFleet.loading')}</p>}

      {answered && roster.length > 0 && (
        <section className="tn-fleet-section" aria-labelledby="tn-fleet-roster">
          <h2 id="tn-fleet-roster">{t('tnAgentFleet.roster.title')}</h2>
          <p className="tn-fleet-hint">{t('tnAgentFleet.roster.hint')}</p>
          <div className="tn-fleet-roster-head" aria-hidden="true">
            <span>{t('tnAgentFleet.roster.provider')}</span>
            <span>{t('tnAgentFleet.roster.models')}</span>
            <span>{t('tnAgentFleet.roster.agents')}</span>
            <span>{t('tnAgentFleet.roster.health')}</span>
          </div>
          <ul className="tn-fleet-roster">
            {roster.map((row) => {
              const name = row.provider || t('tnAgentFleet.roster.noProvider');
              const selected = Boolean(row.provider) && filters.provider === row.provider;
              return (
                <li
                  key={row.provider || '∅'}
                  className={`tn-fleet-roster-row${selected ? ' tn-fleet-roster-row--selected' : ''}`}
                  data-provider={row.provider}
                >
                  <span className="tn-fleet-roster-provider">
                    {row.provider ? (
                      <button
                        type="button"
                        className="tn-fleet-link tn-fleet-provider"
                        aria-pressed={selected}
                        title={
                          selected
                            ? t('tnAgentFleet.roster.showAll')
                            : t('tnAgentFleet.roster.showOnly', { provider: row.provider })
                        }
                        onClick={() =>
                          setFilters({ ...filters, provider: selected ? '' : row.provider })
                        }
                      >
                        {name}
                      </button>
                    ) : (
                      <span className="tn-fleet-muted">{name}</span>
                    )}
                  </span>
                  <span className="tn-fleet-mono tn-fleet-roster-models">
                    {row.models.length ? row.models.join(' · ') : '—'}
                  </span>
                  <span className="tn-fleet-mono tn-fleet-roster-count">
                    {row.agents}
                    <span className="tn-fleet-muted">
                      {' '}
                      · {t('tnAgentFleet.roster.running')} {row.running}
                    </span>
                  </span>
                  <span className="tn-fleet-roster-health">
                    <BalanceMark state={row.balance} />
                  </span>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {answered && (
        <section className="tn-fleet-section" aria-labelledby="tn-fleet-agents">
          <h2 id="tn-fleet-agents">
            {t('tnAgentFleet.agents.title')}
            <span className="tn-fleet-count">
              {t('tnAgentFleet.agents.count', {
                shown: visible.length,
                total: allAgents.length,
              })}
            </span>
          </h2>
          <table className="tn-fleet-table">
            <thead>
              <tr>
                <th>{col('agent')}</th>
                <th>{col('model')}</th>
                <th>{col('role')}</th>
                <th>{col('worktree')}</th>
                <th>{col('status')}</th>
                <th>{col('elapsed')}</th>
                <th>{col('tail')}</th>
                <th>{col('commits')}</th>
                <th>{col('tests')}</th>
                <th>{col('balance')}</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((agent) => (
                <tr key={agent.id} data-status={agent.status}>
                  <td data-label={col('agent')}>
                    <button
                      type="button"
                      className="tn-fleet-link tn-fleet-agent"
                      aria-expanded={openLogs === agent.id}
                      title={t('tnAgentFleet.logs.open', { agent: agent.id })}
                      onClick={() => setOpenLogs(openLogs === agent.id ? null : agent.id)}
                    >
                      {agent.id}
                    </button>
                  </td>
                  <td data-label={col('model')}>
                    <span className="tn-fleet-cell">
                      {agent.provider && (
                        <span className="tn-fleet-provider">{agent.provider}</span>
                      )}
                      <span className="tn-fleet-mono">{agent.model || '—'}</span>
                    </span>
                  </td>
                  <td data-label={col('role')}>{t(`tnAgentFleet.role.${agent.role}`)}</td>
                  <td data-label={col('worktree')}>
                    <span className="tn-fleet-cell tn-fleet-cell--stack">
                      <span className="tn-fleet-branch">{agent.branch || '—'}</span>
                      <span className="tn-fleet-path">{agent.worktree}</span>
                    </span>
                  </td>
                  <td data-label={col('status')}>
                    <StatusMark status={agent.status} />
                  </td>
                  <td data-label={col('elapsed')} className="tn-fleet-num">
                    {formatElapsed(agent.elapsed_secs)}
                  </td>
                  <td data-label={col('tail')} className="tn-fleet-tail" title={agent.log_tail}>
                    <span className="tn-fleet-cell">{summarizeTail(agent.log_tail)}</span>
                  </td>
                  <td data-label={col('commits')} className="tn-fleet-num">
                    {agent.commits_ahead ?? '—'}
                  </td>
                  <td data-label={col('tests')}>{agent.tests_state ?? '—'}</td>
                  <td data-label={col('balance')}>
                    <BalanceMark state={agent.provider_balance} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {visible.length === 0 && <p className="tn-fleet-note">{t('tnAgentFleet.empty')}</p>}
        </section>
      )}

      {openLogs && (
        <section className="tn-fleet-logs-panel" aria-labelledby="tn-fleet-logs-title">
          <header>
            <h2 id="tn-fleet-logs-title" className="tn-fleet-mono">
              {openLogs}
            </h2>
            <button type="button" className="tn-fleet-button" onClick={() => setOpenLogs(null)}>
              {t('tnAgentFleet.logs.close')}
            </button>
          </header>
          <AgentLogsPanel agentId={openLogs} />
        </section>
      )}

      {fleet.data && (
        <footer className="tn-fleet-foot">
          <span>
            {t('tnAgentFleet.foot.scannedAt', {
              time: new Date(fleet.data.scan_ts).toLocaleTimeString(i18n.language),
            })}
          </span>
          <span>
            {t('tnAgentFleet.foot.stallThreshold', { minutes: fleet.data.stall_threshold_mins })}
          </span>
          <span>
            {t('tnAgentFleet.foot.refreshEvery', { seconds: Math.round(interval / 1000) })}
          </span>
        </footer>
      )}
    </div>
  );
}
