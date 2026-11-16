/**
 * Agents × skills and agents × MCP servers.
 *
 * There is no hub route on this server. The page asks
 * `/api/v1/skills-mcp/status` and, on a 404, names the local CLI instead of
 * drawing an empty fleet that was never read.
 */
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  cellAt,
  cellText,
  envEntries,
  loadSkillsMcp,
  matrixColumns,
  postSkillsMcpAction,
  settleAction,
  SKILLS_STATUS_COMMAND,
  SkillsMcpMissing,
} from './model';
import type { ActionName, ActionRequest, MatrixKind, SkillMatrix, SkillsMcpStatus } from './model';
import './skillsMcp.css';

export interface SkillsMcpPageProps {
  fetcher?: () => Promise<SkillsMcpStatus>;
  onAdopt?: (request: ActionRequest) => Promise<string>;
  onApply?: (request: ActionRequest) => Promise<string>;
  onRollback?: (request: ActionRequest) => Promise<string>;
}

function defaultAdopt(request: ActionRequest): Promise<string> {
  return postSkillsMcpAction('adopt', request);
}

function defaultApply(request: ActionRequest): Promise<string> {
  return postSkillsMcpAction('apply', request);
}

function defaultRollback(request: ActionRequest): Promise<string> {
  return postSkillsMcpAction('rollback', request);
}

function kindLabel(kind: MatrixKind): string {
  return kind === 'skills' ? 'skills' : 'MCP';
}

function MatrixTable({
  title,
  kind,
  matrix,
  diffs,
  pending,
  onRun,
}: {
  title: string;
  kind: MatrixKind;
  matrix: SkillMatrix;
  diffs: Record<string, string>;
  pending: string | null;
  onRun: (action: ActionName, kind: MatrixKind, agent: string) => void;
}) {
  const columns = matrixColumns(matrix);
  const headingId = `sm-matrix-${kind}`;
  return (
    <section className="sm-matrix" aria-labelledby={headingId}>
      <h2 id={headingId}>{title}</h2>
      {matrix.agents.length === 0 ? (
        <p className="sm-note">No agents in this matrix.</p>
      ) : (
        <div className="sm-scroll">
          <table className="sm-table">
            <thead>
              <tr>
                <th scope="col">Agent</th>
                {columns.map((column) => (
                  <th key={column} scope="col">
                    {column}
                  </th>
                ))}
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {matrix.agents.map((agent) => {
                const key = `${kind}:${agent.name}`;
                const label = kindLabel(kind);
                const busy = pending === key;
                return (
                  <tr key={agent.name}>
                    <th scope="row">{agent.name}</th>
                    {columns.map((column) => {
                      const cell = cellAt(agent, column);
                      if (!cell) return <td key={column}>—</td>;
                      return (
                        <td key={column} className={`sm-cell sm-cell--${cell.state}`}>
                          {cellText(cell.state)}
                        </td>
                      );
                    })}
                    <td className="sm-actions-cell">
                      <div className="sm-actions">
                        <button
                          type="button"
                          aria-label={`Adopt ${agent.name} ${label}`}
                          disabled={busy}
                          onClick={() => onRun('adopt', kind, agent.name)}
                        >
                          Adopt
                        </button>
                        <button
                          type="button"
                          aria-label={`Apply ${agent.name} ${label}`}
                          disabled={busy}
                          onClick={() => onRun('apply', kind, agent.name)}
                        >
                          Apply
                        </button>
                        <button
                          type="button"
                          aria-label={`Rollback ${agent.name} ${label}`}
                          disabled={busy}
                          onClick={() => onRun('rollback', kind, agent.name)}
                        >
                          Rollback
                        </button>
                      </div>
                      <EnvList env={agent.env} />
                      <pre className="sm-diff" tabIndex={0} role="group" aria-label={`Diff preview ${agent.name} ${label}`}>
                        {diffs[key] ?? ''}
                      </pre>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function EnvList({ env }: { env?: Record<string, unknown> }) {
  const entries = envEntries(env);
  if (entries.length === 0) return null;
  return (
    <dl className="sm-env">
      {entries.map((entry) => (
        <div key={entry.key}>
          <dt>{entry.key}</dt>
          <dd>{entry.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export default function SkillsMcpPage({
  fetcher,
  onAdopt = defaultAdopt,
  onApply = defaultApply,
  onRollback = defaultRollback,
}: SkillsMcpPageProps) {
  const { t } = useTranslation();
  const [phase, setPhase] = useState<'loading' | 'ready' | 'missing' | 'error'>('loading');
  const [status, setStatus] = useState<SkillsMcpStatus | null>(null);
  const [message, setMessage] = useState('');
  const [diffs, setDiffs] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    loadSkillsMcp(fetcher).then(
      (data) => {
        if (!active) return;
        setStatus(data);
        setPhase('ready');
      },
      (error: unknown) => {
        if (!active) return;
        setStatus(null);
        if (error instanceof SkillsMcpMissing) {
          setPhase('missing');
          return;
        }
        setPhase('error');
        setMessage(error instanceof Error ? error.message : 'Skills & MCP status failed');
      },
    );
    return () => {
      active = false;
    };
  }, [fetcher]);

  async function run(action: ActionName, kind: MatrixKind, agent: string) {
    const key = `${kind}:${agent}`;
    const request: ActionRequest = { kind, agent };
    const handler = action === 'adopt' ? onAdopt : action === 'apply' ? onApply : onRollback;
    setPending(key);
    const diff = await settleAction(() => handler(request));
    setDiffs((current) => ({ ...current, [key]: diff }));
    setPending((current) => (current === key ? null : current));
  }

  return (
    <div className="sm-page">
      <header className="sm-head">
        <h1>{t('sidebar.skillsMcp')}</h1>
        <p>✔ installed · ◆ drifted or conflict · ✖ missing</p>
      </header>

      {phase === 'loading' && <p className="sm-note">Loading Skills & MCP…</p>}
      {phase === 'missing' && (
        <p className="sm-note" role="status">
          The local CLI is the source of truth: <code>{SKILLS_STATUS_COMMAND}</code>
        </p>
      )}
      {phase === 'error' && (
        <p className="sm-note sm-note--bad" role="alert">
          {message}
        </p>
      )}

      {phase === 'ready' && status && (
        <>
          <MatrixTable
            title="Agents × skills"
            kind="skills"
            matrix={status.skills}
            diffs={diffs}
            pending={pending}
            onRun={(action, kind, agent) => {
              void run(action, kind, agent);
            }}
          />
          <MatrixTable
            title="Agents × MCP servers"
            kind="mcp"
            matrix={status.mcp}
            diffs={diffs}
            pending={pending}
            onRun={(action, kind, agent) => {
              void run(action, kind, agent);
            }}
          />
        </>
      )}
    </div>
  );
}
