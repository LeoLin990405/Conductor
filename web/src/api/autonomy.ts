/** Super-agent autonomy REST client. */
import { api } from '@/api/client';

export interface AutonomySource {
  source_id: string;
  user_id: string;
  kind: string;
  config: {
    repository: string;
    secret_name: string;
    workspace_id: string;
    allowed_logins: string[];
    bot_logins?: string[];
    poll_seconds: number;
    worker_provider?: string;
    worker_model?: string;
    worker_daemon_id?: string | null;
    chat_id?: string;
    agent_open_id?: string;
    admission?: Record<string, string>;
    api_base_url?: string;
  };
  state: {
    agent_login?: string;
    repository?: string;
    cursor?: string;
    last_poll_at?: string;
    next_poll_at?: string;
    last_error?: string;
  };
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface AutonomyTicket {
  ticket_id: string;
  user_id: string;
  workspace_id: string;
  title: string;
  scope: string;
  status: string;
  mandate: Record<string, unknown>;
  origin_trigger_id: string | null;
  source_id?: string;
  trigger_id?: string;
  origin_thread_key: string;
  router_thread_id: string | null;
  worker_thread_id: string | null;
  worker_run_id: string | null;
  worker_provider?: string;
  worker_model?: string;
  worker_daemon_id?: string | null;
  attempts: number;
  attention: { question?: string } | null;
  summary: string;
  error: string;
  speculative_outcome?: {
    outcome: string;
    recorded_at?: string;
    evidence?: {
      worker_state?: string;
      worker_summary?: string;
      worker_question?: string;
      actor?: string;
      previous_outcome?: string;
      report_id?: string;
    };
  } | null;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  ended_at?: string | null;
  closed_at?: string | null;
  close_reason?: string;
}

export interface AutonomyLineTicket {
  ticket_id: string;
  title: string;
  status: string;
  owner: string;
  worker_provider: string;
  attempts: number;
  updated_at: string;
  last_activity_at: string | null;
}

export interface AutonomyLineGroup {
  line: {
    line_id: string;
    user_id: string;
    name: string;
    branch: string;
    worktree_path: string;
    status: string;
    created_at: string;
    updated_at: string;
  } | null;
  tickets: AutonomyLineTicket[];
  ticket_count: number;
  open_count: number;
  last_activity_at: string | null;
}

export interface AutonomyTicketLines {
  lines: AutonomyLineGroup[];
  ungrouped: AutonomyLineGroup | null;
}

export async function getAutonomyTicketLines(includeArchived = false): Promise<AutonomyTicketLines> {
  const { data } = await api.get('/api/v1/autonomy/tickets/lines', {
    params: { include_archived: includeArchived },
  });
  return data;
}

export interface TicketDetail extends AutonomyTicket {
  inputs: Array<{ input_id: string; kind: string; text: string; created_at: string }>;
  reports: Array<{
    report_id: string;
    state: string;
    summary: string;
    question: string | null;
    git_check?: 'unsupported_runtime' | null;
    created_at: string;
  }>;
}

export interface AutonomyOverview {
  counts: { attention: number; running: number; queued: number; completed: number; failed: number; blocked?: number };
  sources: number;
  enabled_sources: number;
}

export async function getAutonomyOverview(): Promise<AutonomyOverview> {
  const { data } = await api.get('/api/v1/autonomy/overview');
  return data;
}

export interface UnavailableEval {
  eval_id: string;
  thread_key: string;
  trigger_ids: string[];
  reason: string;
  started_at: string | null;
  decided_at: string | null;
}

export async function getUnavailableEvals(): Promise<{ evals: UnavailableEval[] }> {
  const { data } = await api.get('/api/v1/autonomy/evals');
  return data;
}

export async function requeueEval(evalId: string): Promise<{ status: string }> {
  const { data } = await api.post(`/api/v1/autonomy/evals/${evalId}/requeue`);
  return data;
}

export interface AutonomyMetrics {
  pending_triggers: number;
  pending_threads: number;
  active_evals: number;
  pending_effects: number;
  claimed_effects: number;
  queued_tickets: number;
  running_tickets: number;
  oldest_pending_effect_seconds: number;
  max_workers?: number;
}

export async function getAutonomyMetrics(): Promise<AutonomyMetrics> {
  const { data } = await api.get('/api/v1/autonomy/metrics');
  return data;
}

export interface AutonomyAdmission {
  listed_addressed: 'explicit' | 'inferred' | 'ignore';
  listed_unaddressed: 'inferred' | 'ignore';
  unlisted_addressed: 'explicit' | 'inferred' | 'ignore';
  unlisted_unaddressed: 'inferred' | 'ignore';
  bot: 'inferred' | 'ignore';
}

export interface AutonomyPolicy {
  accept_mode: 'explicit' | 'observe' | 'auto';
  version: string;
  rules: Array<Record<string, unknown>>;
  admission: AutonomyAdmission;
}

export async function getAutonomyPolicy(): Promise<AutonomyPolicy> {
  const { data } = await api.get('/api/v1/autonomy/policy');
  return data;
}

export async function updateAutonomyPolicy(input: {
  accept_mode?: AutonomyPolicy['accept_mode'];
  admission?: Partial<AutonomyAdmission>;
}): Promise<AutonomyPolicy> {
  const { data } = await api.patch('/api/v1/autonomy/policy', input);
  return data;
}

export async function listAutonomySources(): Promise<{ sources: AutonomySource[] }> {
  const { data } = await api.get('/api/v1/autonomy/sources');
  return data;
}

export interface AutonomySourceInput {
  kind: 'github' | 'feishu';
  config: {
    repository?: string;
    secret_name?: string;
    chat_id?: string;
    agent_open_id?: string;
    workspace_id: string;
    allowed_logins: string[];
    bot_logins?: string[];
    poll_seconds: number;
    worker_provider?: string;
    worker_model?: string;
    worker_daemon_id?: string | null;
    admission?: Record<string, string>;
    api_base_url?: string;
  };
  enabled?: boolean;
}

export async function createAutonomySource(input: AutonomySourceInput): Promise<AutonomySource> {
  const { data } = await api.post('/api/v1/autonomy/sources', input);
  return data;
}

export async function updateAutonomySource(
  sourceId: string,
  input: {
    enabled?: boolean;
    config?: AutonomySourceInput['config'];
  },
): Promise<AutonomySource> {
  const { data } = await api.patch(`/api/v1/autonomy/sources/${sourceId}`, input);
  return data;
}

export async function deleteAutonomySource(sourceId: string): Promise<void> {
  await api.delete(`/api/v1/autonomy/sources/${sourceId}`);
}

export async function pollAutonomySource(sourceId: string): Promise<AutonomySource> {
  const { data } = await api.post(`/api/v1/autonomy/sources/${sourceId}/poll`);
  return data;
}

export async function checkAutonomySource(sourceId: string): Promise<{
  ok: boolean;
  error?: string;
  agent_login?: string;
  repository?: string;
  can_push?: boolean;
}> {
  const { data } = await api.post(`/api/v1/autonomy/sources/${sourceId}/check`);
  return data;
}

export async function listAutonomyTickets(
  filters: {
    status?: string;
    source_id?: string;
    schedule_id?: string;
    failed?: boolean;
    attention?: boolean;
    q?: string;
    sort?: 'updated' | 'created' | 'title';
    limit?: number;
    offset?: number;
  } = {},
): Promise<{ tickets: AutonomyTicket[] }> {
  const { data } = await api.get('/api/v1/autonomy/tickets', {
    params: { ...filters, limit: filters.limit ?? 200 },
  });
  return data;
}

export async function getAutonomyTicket(ticketId: string): Promise<TicketDetail> {
  const { data } = await api.get(`/api/v1/autonomy/tickets/${ticketId}`);
  return data;
}

/** Phases `build_ticket_progress` maps onto. `blocked` is `waiting`; `archived` is `cancelled`. */
export type TicketProgressPhase =
  | 'queued'
  | 'running'
  | 'waiting'
  | 'completed'
  | 'failed'
  | 'cancelled';

/**
 * Step state from `progress.py`. Later stages are omitted, not marked pending.
 * A failed phase marks the worker and report steps `failed`.
 */
export type TicketProgressStepState = 'done' | 'active' | 'failed';

export interface TicketProgressStep {
  key: string;
  label: string;
  state: TicketProgressStepState;
}

export interface TicketProgress {
  ticket_id: string;
  /** Ticket status before phase mapping. */
  status: string;
  /**
   * Supervisor phase. An unknown ticket status is returned unchanged, so this
   * is a string even though the mapped values are {@link TicketProgressPhase}.
   */
  phase: TicketProgressPhase | (string & {});
  elapsed_seconds: number;
  steps: TicketProgressStep[];
  summary: string;
  /** Full user-facing report, absent on legacy progress payloads. */
  body?: string | null;
  question: string;
  error: string;
}

function rejectedStatus(err: unknown): number | null {
  if (typeof err !== 'object' || err === null) return null;
  const row = err as { status?: unknown; response?: { status?: unknown } };
  const status = row.response?.status ?? row.status;
  return typeof status === 'number' ? status : null;
}

/**
 * Supervisor progress for one ticket. Feature off, or a ticket the user does
 * not own, is HTTP 404 and comes back as null. Any other failure is thrown.
 */
export async function getTicketProgress(ticketId: string): Promise<TicketProgress | null> {
  try {
    const { data } = await api.get<TicketProgress>(`/api/v1/autonomy/tickets/${ticketId}/progress`);
    return data;
  } catch (err) {
    if (rejectedStatus(err) === 404) return null;
    throw err;
  }
}

export async function stopAutonomyTicket(ticketId: string): Promise<AutonomyTicket> {
  const { data } = await api.post(`/api/v1/autonomy/tickets/${ticketId}/stop`);
  return data;
}

export interface TaskActivityEvent {
  event_id: string;
  kind: string;
  at: string;
  summary: string;
  detail: string;
  state: string;
}

export interface TaskActivity {
  ticket_id: string;
  events: TaskActivityEvent[];
}

export async function getTaskActivity(ticketId: string): Promise<TaskActivity> {
  const { data } = await api.get(`/api/v1/autonomy/tickets/${ticketId}/activity`);
  return data;
}

export async function closeAutonomyTicket(ticketId: string, reason = ''): Promise<AutonomyTicket> {
  const { data } = await api.post(`/api/v1/autonomy/tickets/${ticketId}/close`, { reason });
  return data;
}

export async function retryAutonomyTicket(ticketId: string): Promise<AutonomyTicket> {
  const { data } = await api.post(`/api/v1/autonomy/tickets/${ticketId}/retry`);
  return data;
}

export async function discardAutonomyTicket(ticketId: string): Promise<AutonomyTicket> {
  const { data } = await api.post(`/api/v1/autonomy/tickets/${ticketId}/discard`);
  return data;
}

export async function replyAutonomyTicket(ticketId: string, text: string): Promise<unknown> {
  const { data } = await api.post(`/api/v1/autonomy/tickets/${ticketId}/reply`, { text });
  return data;
}

export interface TicketClarification {
  clarification_id: string;
  ticket_id: string;
  status: string;
  clarity_score: number;
  original_prompt: string;
  expanded_request: string;
  assumptions: string[];
  open_questions: string[];
  confirmed_text: string | null;
  created_at: string;
}

export async function getTicketClarification(ticketId: string): Promise<TicketClarification> {
  const { data } = await api.get(`/api/v1/autonomy/tickets/${ticketId}/clarification`);
  return data;
}

export async function confirmTicketClarification(ticketId: string, text: string): Promise<AutonomyTicket> {
  const { data } = await api.post(`/api/v1/autonomy/tickets/${ticketId}/clarification/confirm`, { text });
  return data;
}

export interface RawRunFrame {
  id: string;
  data: string;
}

export async function getAutonomyTicketRaw(ticketId: string): Promise<{
  thread_id: string | null;
  run_id: string | null;
  events: RawRunFrame[];
  note: string | null;
}> {
  const { data } = await api.get(`/api/v1/autonomy/tickets/${ticketId}/raw`);
  return data;
}

export interface ManualTaskInput {
  prompt: string;
  title?: string;
  workspace_id: string;
  worker_provider?: string;
  worker_model?: string;
  worker_daemon_id?: string;
}

export interface AutonomyDaemon {
  daemon_id: string;
  name: string;
  provider: string;
  max_concurrency: number;
  last_seen_at: string | null;
  created_at: string;
}

export async function listAutonomyDaemons(): Promise<{ daemons: AutonomyDaemon[] }> {
  const { data } = await api.get('/api/v1/autonomy/daemons');
  return data;
}

export async function createAutonomyDaemon(input: {
  name: string;
  provider: string;
  max_concurrency?: number;
}): Promise<AutonomyDaemon & { token: string }> {
  const { data } = await api.post('/api/v1/autonomy/daemons', input);
  return data;
}

export async function updateAutonomyDaemon(
  daemonId: string,
  input: { max_concurrency: number },
): Promise<AutonomyDaemon> {
  const { data } = await api.patch(`/api/v1/autonomy/daemons/${daemonId}`, input);
  return data;
}

export async function deleteAutonomyDaemon(daemonId: string): Promise<void> {
  await api.delete(`/api/v1/autonomy/daemons/${daemonId}`);
}

export async function createManualTask(input: ManualTaskInput): Promise<AutonomyTicket> {
  const { data } = await api.post('/api/v1/autonomy/tasks', input);
  return data;
}

export interface AutonomySchedule {
  schedule_id: string;
  user_id: string;
  workspace_id: string;
  title: string;
  prompt: string;
  cron_expression: string;
  timezone: string;
  enabled: boolean;
  worker_provider: string;
  worker_model: string;
  worker_daemon_id?: string | null;
  next_run_at: string;
  last_run_at: string | null;
  last_error: string;
  created_at: string;
  updated_at: string;
}

export interface ScheduleInput {
  prompt: string;
  title?: string;
  workspace_id: string;
  cron_expression: string;
  timezone: string;
  worker_provider?: string;
  worker_model?: string;
  worker_daemon_id?: string | null;
  enabled?: boolean;
}

export async function listAutonomySchedules(): Promise<{ schedules: AutonomySchedule[] }> {
  const { data } = await api.get('/api/v1/autonomy/schedules');
  return data;
}

export async function createAutonomySchedule(input: ScheduleInput): Promise<AutonomySchedule> {
  const { data } = await api.post('/api/v1/autonomy/schedules', input);
  return data;
}

export async function runAutonomySchedule(scheduleId: string): Promise<AutonomyTicket> {
  const { data } = await api.post(`/api/v1/autonomy/schedules/${scheduleId}/run`);
  return data;
}

export async function updateAutonomySchedule(
  scheduleId: string,
  input: Partial<ScheduleInput>,
): Promise<AutonomySchedule> {
  const { data } = await api.patch(`/api/v1/autonomy/schedules/${scheduleId}`, input);
  return data;
}

export async function deleteAutonomySchedule(scheduleId: string): Promise<void> {
  await api.delete(`/api/v1/autonomy/schedules/${scheduleId}`);
}
