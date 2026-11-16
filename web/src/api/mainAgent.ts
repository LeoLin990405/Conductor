import { api } from '@/api/client';

export interface MainAgentStatus {
  run_id: string;
  task_id: string;
  ticket_id: string | null;
  agent_id: string | null;
  daemon_id: string | null;
  state: string;
  last_heartbeat: string | null;
  current_step: string | null;
  stalled: boolean;
  stall_reason: string | null;
}

export interface MainAgentAlert {
  id: string;
  run_id?: string | null;
  task_id?: string | null;
  severity?: string | null;
  message: string;
  created_at?: string | null;
  [key: string]: unknown;
}

export interface MainAgentStatusBoard {
  agents: MainAgentStatus[];
  alerts: Array<MainAgentAlert | string>;
  thresholds: {
    heartbeat_seconds: number;
    progress_seconds: number;
  };
}

export async function getMainAgentStatusBoard(): Promise<MainAgentStatusBoard> {
  const { data } = await api.get<MainAgentStatusBoard>('/api/v1/main-agent/status-board');
  return data;
}
