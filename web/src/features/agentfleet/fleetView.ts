/**
 * Pure view helpers for the agent fleet table.
 *
 * Kept out of the component so the filter, sort and formatting rules are
 * testable without a DOM, and so the CLI and the dashboard agree on the same
 * column semantics.
 */
import type { AgentRole, AgentStatus, BalanceState, FleetAgent } from '@/api/agentFleet';

export type SortKey = 'id' | 'elapsed' | 'commits' | 'status' | 'role' | 'model';

export const SORT_KEYS: SortKey[] = ['id', 'elapsed', 'commits', 'status', 'role', 'model'];

/** Severity order an operator scans by: live work first, finished work last. */
const STATUS_RANK: Record<AgentStatus, number> = {
  running: 0,
  error: 1,
  stalled: 2,
  unknown: 3,
  done: 4,
};

/** Terminal-native state ink (SPEC.md "Status"); never depends on the locale. */
export type MarkInk = 'tempo' | 'hold' | 'error' | 'ok' | 'muted';

export interface Mark {
  glyph: string;
  ink: MarkInk;
}

/**
 * A glyph and an ink for every agent status. The word comes from the locale
 * (`tnAgentFleet.status.*`), so a state is never told by colour alone.
 */
export const STATUS_MARKS: Record<AgentStatus, Mark> = {
  running: { glyph: '●', ink: 'tempo' },
  stalled: { glyph: '▣', ink: 'hold' },
  error: { glyph: '✖', ink: 'error' },
  done: { glyph: '✔', ink: 'ok' },
  unknown: { glyph: '○', ink: 'muted' },
};

/** Provider headroom as a health mark; the word is `tnAgentFleet.balance.*`. */
export const BALANCE_MARKS: Record<BalanceState, Mark> = {
  ok: { glyph: '✔', ink: 'ok' },
  low: { glyph: '◆', ink: 'hold' },
  unknown: { glyph: '○', ink: 'muted' },
};

export const AGENT_STATUSES: AgentStatus[] = ['running', 'stalled', 'error', 'done', 'unknown'];
export const AGENT_ROLES: AgentRole[] = ['plan', 'dev', 'review', 'research'];

export interface FleetFilters {
  status: AgentStatus | 'all';
  role: AgentRole | 'all';
  provider: string;
  model: string;
}

export const EMPTY_FILTERS: FleetFilters = {
  status: 'all',
  role: 'all',
  provider: '',
  model: '',
};

export function formatElapsed(seconds: number | null): string {
  if (typeof seconds !== 'number' || seconds < 0) return '—';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  const pad = (value: number) => String(value).padStart(2, '0');
  if (days > 0) return `${days}d ${pad(hours)}h`;
  if (hours > 0) return `${hours}:${pad(minutes)}:${pad(rest)}`;
  return `${pad(minutes)}:${pad(rest)}`;
}

export function filterAgents(agents: FleetAgent[], filters: FleetFilters): FleetAgent[] {
  const provider = filters.provider.trim().toLowerCase();
  const model = filters.model.trim().toLowerCase();
  return agents.filter((agent) => {
    if (filters.status !== 'all' && agent.status !== filters.status) return false;
    if (filters.role !== 'all' && agent.role !== filters.role) return false;
    if (provider && !agent.provider.toLowerCase().includes(provider)) return false;
    if (model && !agent.model.toLowerCase().includes(model)) return false;
    return true;
  });
}

export function sortAgents(agents: FleetAgent[], key: SortKey): FleetAgent[] {
  const rows = [...agents];
  rows.sort((left, right) => {
    switch (key) {
      case 'elapsed': {
        // Longest-running first; an agent with no elapsed time sorts last.
        const delta = (right.elapsed_secs ?? -1) - (left.elapsed_secs ?? -1);
        return delta || left.id.localeCompare(right.id);
      }
      case 'commits': {
        const delta = (right.commits_ahead ?? -1) - (left.commits_ahead ?? -1);
        return delta || left.id.localeCompare(right.id);
      }
      case 'status':
        return (
          STATUS_RANK[left.status] - STATUS_RANK[right.status] ||
          left.id.localeCompare(right.id)
        );
      case 'role':
        return left.role.localeCompare(right.role) || left.id.localeCompare(right.id);
      case 'model':
        return left.model.localeCompare(right.model) || left.id.localeCompare(right.id);
      default:
        return left.id.localeCompare(right.id);
    }
  });
  return rows;
}

/** One-line log summary for the table cell; the text is already redacted. */
export function summarizeTail(tail: string, maxLength = 80): string {
  const flat = tail.replace(/\s+/g, ' ').trim();
  if (!flat) return '—';
  return flat.length > maxLength ? `${flat.slice(0, maxLength - 1)}…` : flat;
}

/** Auto-refresh interval from `?refresh=<seconds>`, clamped to a sane range. */
export function refreshIntervalFromQuery(search: string, fallback = 30_000): number {
  const raw = new URLSearchParams(search).get('refresh');
  if (raw === null) return fallback;
  const seconds = Number(raw);
  if (!Number.isFinite(seconds) || seconds < 0) return fallback;
  return Math.min(Math.round(seconds * 1000), 600_000);
}

export interface ProviderRow {
  /** The provider id exactly as the scan reported it ('' when it had none). */
  provider: string;
  /** Distinct non-empty models seen for this provider, sorted. */
  models: string[];
  agents: number;
  running: number;
  /** Worst headroom across the provider's agents: low > unknown > ok. */
  balance: BalanceState;
}

const BALANCE_RANK: Record<BalanceState, number> = { ok: 0, unknown: 1, low: 2 };

/**
 * The provider roster: one row per provider in the scan, in name order. The
 * provider names are data (whatever the backend reports), never a fixed list.
 */
export function providerRoster(agents: FleetAgent[]): ProviderRow[] {
  const byProvider = new Map<string, { models: Set<string>; row: ProviderRow }>();
  for (const agent of agents) {
    const key = agent.provider ?? '';
    let entry = byProvider.get(key);
    if (!entry) {
      entry = {
        models: new Set(),
        row: { provider: key, models: [], agents: 0, running: 0, balance: agent.provider_balance },
      };
      byProvider.set(key, entry);
    }
    entry.row.agents += 1;
    if (agent.status === 'running') entry.row.running += 1;
    if (agent.model) entry.models.add(agent.model);
    if (BALANCE_RANK[agent.provider_balance] > BALANCE_RANK[entry.row.balance]) {
      entry.row.balance = agent.provider_balance;
    }
  }
  return [...byProvider.values()]
    .map(({ models, row }) => ({ ...row, models: [...models].sort() }))
    .sort((left, right) => left.provider.localeCompare(right.provider));
}
