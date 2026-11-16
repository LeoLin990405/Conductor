/** Core API types — User, Workspace, Thread, and response wrappers */

// --- User ---

export interface User {
  user_id: string;
  email: string;
  name?: string | null;
  avatar_url?: string | null;
  timezone?: string | null;
  locale?: string | null;
  has_api_key?: boolean;
  has_oauth_token?: boolean;
  access_tier?: number;
  plan_display_name?: string | null;
  /** Completed the personalization flow (i.e. configured a BYOK key). */
  personalization_completed?: boolean;
  /** Completed the legacy first-run onboarding flow. */
  onboarding_completed?: boolean;
  created_at?: string;
  updated_at?: string;
  [key: string]: unknown;
}

export interface UserPreferences {
  [key: string]: unknown;
}

// --- Feature flags ---

/**
 * One feature flag as seen by the current user. `enabled` is the effective
 * value (user override when allowed + set, else the system default);
 * `user_override` is the raw override (null = unset, follows the default).
 * `gate` is the access model — only `opt_in`/`opt_out` accept user overrides.
 */
export interface FeatureState {
  key: string;
  label: string;
  description: string;
  /** Honest cost of opting in to an experimental feature; English-only, null when none. */
  tradeoffs: string | null;
  enabled: boolean;
  gate: 'none' | 'opt_in' | 'opt_out' | 'plan';
  min_tier: number | null;
  user_override: boolean | null;
}

// --- Workspace ---

/** Sandbox resource tier. */
export type ResourceTier = 'standard' | 'performance' | 'max';

export interface Workspace {
  workspace_id: string;
  name: string;
  /** Lifecycle of the workspace's computer. Home reads `'unbound'` until its
   *  first start or turn puts it on one. */
  status?: string;
  /** The user's Home, where All workspaces runs. Never read Home off `status`. */
  is_home: boolean;
  description?: string;
  config?: Record<string, unknown>;
  /** Sandbox resource tier. Absent on Home and legacy rows. */
  resource_tier?: ResourceTier;
  /** Keep the sandbox running (idle auto-stop disabled). Absent on Home and legacy rows. */
  is_always_on?: boolean;
  /**
   * The machine this workspace lives on. Absent on Home before its first start
   * and on rows that predate the split, which is exactly when the
   * per-workspace status channel stays the only source for this row's state.
   */
  computer_id?: string | null;
  /** Folder name under the computer's root: the workspace's address on disk. */
  dir_name?: string | null;
  /** Folders a rename moved this workspace out of, most recent first. Paths in
   *  older turns still name them. */
  previous_dir_names?: string[];
  /** The restore from this workspace's file backup did not finish, so the tree
   *  is short some files until the next start retries it. */
  files_restore_incomplete?: boolean;
  /** When the server found this workspace's folder gone from a machine the
   *  user brought and held its backup for the user to choose: restore it, or
   *  keep the folder empty. Set, the backup stops following the folder until
   *  the user answers. */
  files_restore_offered_at?: string | null;
  /** When the last complete backup pass ran; null when unknown. */
  files_backed_up_at?: string | null;
  created_at?: string;
  updated_at?: string;
  [key: string]: unknown;
}

export interface WorkspacesResponse {
  workspaces: Workspace[];
  total?: number;
}

/** Request body for `POST /api/v1/workspaces`. */
export interface WorkspaceCreate {
  name: string;
  description?: string;
  config?: Record<string, unknown>;
  /** The caller's live computer to put the folder on. Omitted means the primary. */
  computer_id?: string;
}

/**
 * Payload of a `file_operation` artifact event. `file_path` is workspace
 * relative, the spelling every path helper classifies; `sandbox_path` is the
 * one the tool was called with, kept for opening the file where it sits.
 */
export interface FileOperationArtifactPayload {
  operation: 'Write' | 'Edit' | string;
  file_path: string;
  sandbox_path?: string;
  line_count?: number;
  content?: string;
  old_string?: string;
  new_string?: string;
  error?: string;
}

export interface ReorderItem {
  workspace_id: string;
  position: number;
}

// --- Thread ---

/** An agent that hands turns to a workspace's thread. */
export type DispatchingAgent = 'chief_of_staff';

/**
 * Who initiated a thread, or one turn of it (a query row's metadata carries
 * its own); absent origin (or empty metadata) = user-initiated.
 */
export interface ThreadOrigin {
  type: 'agent' | 'automation' | 'system';
  /** agent → dispatching thread id; automation → automation id */
  id?: string;
  /** automation → its name when the turn was sent. Display only. */
  name?: string;
  /** agent → which agent dispatched it. Absent on rows from before it was recorded. */
  agent?: DispatchingAgent;
}

/**
 * What a report-back turn reports on, stamped on its query row. The turn's own
 * message is written for the model, so history labels the turn from this
 * instead. Shares drop it. Mirrors `ReportSource` in src/server/models/chat.py.
 */
export type ReportSource =
  /** A dispatched generalist's thread reporting back, and its workspace. */
  | { kind: 'generalist'; thread_id: string; workspace_id?: string; workspace_name?: string }
  /** A background subagent task in this thread, finished or failed. */
  | { kind: 'task'; task_id: string; display_id: string; description?: string; failed: boolean };

/** Immutable binding written when a thread is created on a native CLI lane. */
export interface NativeRuntimeBinding {
  provider: string;
  session_id?: string | null;
  cwd?: string | null;
  model?: string | null;
  version?: number;
}

export interface ThreadMetadata {
  origin?: ThreadOrigin;
  native_runtime?: NativeRuntimeBinding;
  [key: string]: unknown;
}

export interface Thread {
  workspace_id: string;
  thread_id: string;
  title: string | null;
  metadata?: ThreadMetadata;
  /** Pinned threads sort first within their workspace. */
  is_pinned?: boolean;
  /** Archive stamp; null/absent = active. Archived rows only appear when explicitly requested. */
  archived_at?: string | null;
  /** The model this thread runs on; null follows the account default for its mode. */
  llm_model?: string | null;
  /** Whether the agent may hand work to subagents; null (or absent, from an older
   *  server) follows the user's subagents default. */
  subagents_allowed?: boolean | null;
  /** Swarm mode persisted on the thread. */
  swarm_enabled?: boolean;
  /** The thread's own reasoning effort; null (or absent, from an older server)
   *  follows Settings for the thread's model. */
  reasoning_effort?: string | null;
  /** The thread's own fast mode; null (or absent) follows Settings. */
  fast_mode?: boolean | null;
  /** Turn count (list responses only). */
  turn_count?: number;
  created_at?: string;
  updated_at?: string;
  // Lifecycle enrichment (present on list responses for threads with runs).
  /** Public status of the latest run: running|stopping|recovering|queued|completed|interrupted|failed|cancelled */
  run_status?: string;
  interrupt_reason?: string | null;
  latest_run_seq?: number;
  latest_run_id?: string | null;
  last_seen_run_seq?: number;
  run_started_at?: string | null;
  [key: string]: unknown;
}

export interface ThreadsResponse {
  threads: Thread[];
  total: number;
  limit: number;
  offset: number;
}

export interface DeleteThreadResponse {
  success: boolean;
  thread_id: string;
  message: string;
}

/**
 * The backend's public workflow-run status vocabulary — the `status` field of the
 * `/status` and dispatch-liveness responses. Single source of truth for the wire
 * spellings: `idle` = not yet registered; `queued`/`recovering` = registered but
 * no output yet; `stopping` = a live run being cancelled. Consumers that read
 * untrusted wire values still take `unknown` and narrow against these members.
 */
export type WorkflowRunStatus =
  | 'idle'
  | 'queued'
  | 'running'
  | 'stopping'
  | 'recovering'
  | 'completed'
  | 'interrupted'
  | 'failed'
  | 'cancelled';

// --- Thread Sharing ---

export interface ThreadSharePermissions {
  allow_files?: boolean;
  allow_download?: boolean;
  root_path?: string;
}

export interface ThreadShareStatus {
  is_shared: boolean;
  share_token: string;
  share_url: string;
  permissions: ThreadSharePermissions;
}

// --- Share links (files and apps) ---

export type ShareLinkKind = 'file' | 'app';

interface ShareLinkBase {
  code: string;
  /** The workspace the item lives in. */
  workspace_id: string;
  /** App-relative, `/a/<code>`. */
  url: string;
  title: string | null;
  shared: boolean;
  shared_at: string | null;
  /** The confirmed list while shared, null otherwise. */
  shared_files: string[] | null;
  created_at: string;
}

export interface FileShareLink extends ShareLinkBase {
  kind: 'file';
  /** Workspace-relative entry path. */
  path: string;
  port: null;
}

export interface AppShareLink extends ShareLinkBase {
  kind: 'app';
  /** The optional entry path on the served app. */
  path: string | null;
  port: number;
}

/** One item's stable `/a/<code>` link. Private unless `shared`. */
export type ShareLink = FileShareLink | AppShareLink;

export type ShareLinkTarget =
  | { kind: 'file'; path: string }
  | { kind: 'app'; port: number };

export type ShareFileReason = 'entry' | 'page' | 'style' | 'markdown' | 'script';

export interface ShareFileEntry {
  path: string;
  size: number;
  reason: ShareFileReason;
}

export interface ShareLinkDrift {
  added: string[];
  removed: string[];
}

/** The current file list for a file link, with the drift against what was confirmed. */
export interface ShareLinkFiles {
  files: ShareFileEntry[];
  total_size: number;
  drift: ShareLinkDrift | null;
}

export interface SharedLinksResponse {
  links: ShareLink[];
}

/** A signed, expiring prefix the owner's iframes serve workspace files under. */
export interface FileGrant {
  /** `/api/v1/wsfiles/g/<grant>/`, relative to the API base. */
  prefix: string;
  /** Seconds the grant has left when it is answered. */
  expires_in: number;
}

// --- Workspace Files ---

export interface WorkspaceFile {
  name: string;
  path: string;
  type: 'file' | 'directory';
  size?: number;
  modified?: string;
  [key: string]: unknown;
}

export interface ListFilesResponse {
  workspace_id: string;
  path: string;
  files: WorkspaceFile[];
}

export interface ReadFileResponse {
  workspace_id: string;
  path: string;
  content: string;
  mime: string;
  truncated: boolean;
}

export interface WriteFileResponse {
  workspace_id: string;
  path: string;
  size: number;
}

export type UnsavedReason = 'too_large' | 'path_too_long' | 'unreadable' | 'changed' | 'failed';

export interface UnsavedFile {
  path: string;
  reason: UnsavedReason;
  size?: number | null;
}

export interface BackupResponse {
  workspace_id: string;
  synced: number;
  skipped: number;
  deleted: number;
  errors: number;
  oversized: number;
  total_size: number;
  max_file_bytes: number | null;
  /** At most the first 100; `unsaved_count` is the full total. */
  unsaved: UnsavedFile[];
  unsaved_count: number;
}

export interface BackupStatusResponse {
  workspace_id: string;
  backed_up: string[];
  modified: string[];
  untracked: string[];
  total_backed_up_size: number;
  files_restore_incomplete: boolean;
  /** The computer's backup switch. */
  files_backup?: boolean;
  /** As on the workspace row: a restore offer waiting on the user. */
  files_restore_offered_at?: string | null;
}

// --- Subagent ---

export interface SubagentMessageResponse {
  success: boolean;
  tool_call_id: string;
  display_id: string;
  queue_position: number;
}

// --- Feedback ---

export interface FeedbackPayload {
  turn_index: number;
  rating: number;
  issue_categories?: string[] | null;
  comment?: string | null;
  consent_human_review?: boolean;
}

// --- OAuth ---

export interface OAuthStatus {
  connected: boolean;
  account_id: string | null;
  email: string | null;
  plan_type: string | null;
}

export interface CodexDeviceInitResponse {
  user_code: string;
  verification_url: string;
  interval: number;
}

export type CodexDevicePollResponse =
  | { pending: true }
  | { success: true; email: string; plan_type: string; account_id: string };

// --- SSE Streaming ---

export interface StreamFetchResult {
  disconnected: boolean;
}

export type SSEEventCallback = (event: SSEEventData) => void;

export interface SSEEventData {
  event?: string;
  agent?: string;
  content?: string;
  timestamp?: string | number;
  metadata?: Record<string, unknown>;
  _eventId?: number | string;
  [key: string]: unknown;
}

// --- Chat Message Send Body ---

export interface ChatMessageBody {
  workspace_id: string;
  messages: Array<{ role: string; content: string }>;
  locale: string;
  timezone: string;
  additional_context?: unknown;
  checkpoint_id?: string;
  fork_from_turn?: number;
  expected_latest_run_id?: string;
  llm_model?: string;
  reasoning_effort?: string;
  fast_mode?: true;
  hitl_response?: HitlResponseBody;
}

/** One answer on a resume: the verdict, plus the user's own message if they
 *  typed one. */
export interface HitlDecisionBody {
  type: string;
  message?: string;
}

/**
 * What one interrupt is answered with: one decision per request the interrupt
 * raised, in the order it raised them.
 */
export interface HitlResumeEntry {
  decisions: HitlDecisionBody[];
}

/** The `hitl_response` map a resume sends, keyed by interrupt id. */
export type HitlResponseBody = Record<string, HitlResumeEntry>;

/** One thread's live goal (Phase-3 shell read). Fields not listed stay absent:
 *  the server whitelists them, so a new column never silently reaches the UI. */
export interface ThreadGoalView {
  goal_id: string;
  thread_id: string;
  workspace_id: string;
  title: string;
  objective: string;
  status: string;
  stop_conditions?: unknown;
  budget?: unknown;
  verifier_verdict?: unknown;
  created_at?: string;
  updated_at?: string;
  completed_at?: string | null;
}

/** The latest plan for a goal. `status` moves draft -> awaiting_approval ->
 *  approved/rejected/superseded; the shell renders it read-only for now. */
export interface ThreadPlanView {
  plan_id: string;
  version: number;
  status: string;
  steps?: unknown;
  feedback?: string | null;
  created_at?: string;
  decided_at?: string | null;
}

/** One node of the main agent's persisted task tree. */
export interface ThreadGoalTaskNode {
  task_node_id: string;
  title: string;
  status: string;
  parent_node_id?: string | null;
  source?: string;
  children?: ThreadGoalTaskNode[];
}

/** `GET /api/v1/threads/{id}/goal`: flag-shaped snapshot for the shell.
 *  `enabled` is the deployment's goals flag, not "a goal exists". */
export interface ThreadGoalResponse {
  enabled: boolean;
  goal: ThreadGoalView | null;
  plan: ThreadPlanView | null;
  tasks: { enabled: boolean; roots: ThreadGoalTaskNode[] };
}

/** One row of the thread's append-only refactor audit trail. */
export interface ThreadEventView {
  event_id: number;
  thread_id?: string | null;
  response_id?: string | null;
  task_run_id?: string | null;
  event_type: string;
  payload?: Record<string, unknown>;
  created_at?: string;
}

/** `GET /api/v1/threads/{id}/events`: a flag-shaped page of the audit trail. */
export interface ThreadEventsResponse {
  enabled: boolean;
  events: ThreadEventView[];
}

/** One active memory-projection row for the inspector. */
export interface MemoryIndexEntry {
  tier: string;
  entry_key: string;
  index_text: string;
  status: string;
  provenance?: Record<string, unknown> | null;
  updated_at?: string | null;
}

/** `GET /api/v1/memory/index`: flag-shaped page of the user's projection. */
export interface MemoryIndexResponse {
  enabled: boolean;
  tier: string | null;
  entries: MemoryIndexEntry[];
}

/** `POST /api/v1/threads/{id}/goal`: the created live goal + latest plan. */
export interface ThreadGoalCreateResponse {
  enabled: boolean;
  goal: ThreadGoalView | null;
  plan: ThreadPlanView | null;
}

/** What the shell can set on a goal (stop conditions are required). */
export interface GoalCreateInput {
  title: string;
  objective: string;
  stop_conditions: string[];
}

/** `GET/PUT /api/v1/threads/{id}/permission`: the thread's D3 mode. */
export interface ThreadPermissionResponse {
  enabled: boolean;
  mode: 'manual' | 'auto';
}

/** One proposed memory extracted from a finished subagent contract (D5). */
export interface MemoryProposalView {
  proposal_id?: string | null;
  type: string;
  text: string;
  task_run_id?: string | null;
  thread_id?: string | null;
  task_id?: string | null;
  status?: string | null;
  proposed_at?: string | null;
}

/** `GET /api/v1/memory/proposals`: read-only proposal channel. */
export interface MemoryProposalsResponse {
  enabled: boolean;
  proposals: MemoryProposalView[];
}

/** `POST /api/v1/memory/proposals/{id}/decision`: the moved proposal. */
export interface MemoryProposalDecisionResponse {
  enabled: boolean;
  proposal: MemoryProposalView | null;
}
