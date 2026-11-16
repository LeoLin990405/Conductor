/**
 * Local agent fleet dashboard.
 *
 * The backend scans the host's worktrees, processes and agent logs read-only
 * and returns one row per agent. Log text arrives already redacted, so this
 * module never post-processes it, and the balance column is a three-valued
 * state — no amount, account or identity ever reaches the client.
 */
import { api } from '@/api/client';

export type AgentStatus = 'running' | 'stalled' | 'error' | 'done' | 'unknown';
export type AgentRole = 'plan' | 'dev' | 'review' | 'research';
export type BalanceState = 'ok' | 'low' | 'unknown';

export interface FleetAgent {
  id: string;
  model: string;
  provider: string;
  role: AgentRole;
  pid: number | null;
  worktree: string;
  branch: string;
  status: AgentStatus;
  elapsed_secs: number | null;
  last_activity_ts: string | null;
  log_file: string;
  log_tail: string;
  commits_ahead: number | null;
  tests_state: string | null;
  provider_balance: BalanceState;
}

export interface AgentFleet {
  agents: FleetAgent[];
  scan_ts: string;
  stall_threshold_mins: number;
  error: string | null;
}

export interface AgentLogs {
  agent_id: string;
  log_file: string;
  lines: string[];
  truncated: boolean;
}

export async function getAgentFleet(): Promise<AgentFleet> {
  const { data } = await api.get<AgentFleet>('/api/v1/agents/fleet');
  return data;
}

/** A bounded, already-redacted slice of one agent's log. */
export async function getAgentLogs(agentId: string, lines = 100): Promise<AgentLogs> {
  const { data } = await api.get<AgentLogs>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/logs`,
    { params: { lines } },
  );
  return data;
}
