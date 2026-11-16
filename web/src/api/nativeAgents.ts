/**
 * Host-native runtime inventory.
 *
 * The chat composer must only offer a CLI the backend host can actually run.
 * `installed` is binary presence; `live_verified` is the backend's recorded
 * live proof. Capabilities are carried through so the UI never implies HITL
 * approvals or true token streaming on a provider that only has chunked events.
 */
import { api } from '@/api/client';

export interface NativeRuntimeCapabilities {
  streaming: boolean;
  tool_events: boolean;
  session_resume: boolean;
  cache_usage: boolean;
  approvals: boolean;
  mcp: boolean;
  memory: boolean;
  skills: boolean;
  workspace_files: boolean;
  cancel: boolean;
}

export interface NativeRuntimeStatus {
  provider: string;
  installed: boolean;
  binary: string | null;
  live_verified: boolean;
  capabilities: NativeRuntimeCapabilities;
  error: string | null;
}

interface NativeAgentsResponse {
  runtimes: NativeRuntimeStatus[];
}

export async function getNativeAgents(): Promise<NativeRuntimeStatus[]> {
  const { data } = await api.get<NativeAgentsResponse>('/api/v1/native-agents');
  return data.runtimes;
}

/** A composer option: only a host-verified runtime is selectable. */
export function selectableNativeAgents(
  runtimes: NativeRuntimeStatus[] | undefined,
): NativeRuntimeStatus[] {
  return (runtimes ?? []).filter(
    (runtime) => runtime.installed && runtime.live_verified,
  );
}

/** Official CLI sign-in status and interactive login sessions. */
export interface NativeAuthStatus {
  provider: string;
  supported: boolean;
  installed: boolean;
  authenticated: boolean | null;
}

export interface NativeLoginSession {
  session_id: string;
  provider: string;
  status: 'waiting' | 'input_required' | 'authenticated' | 'failed' | 'cancelled' | 'expired';
  auth_url: string | null;
  requires_input: boolean;
  instruction: string;
  error: string | null;
  created_at?: number | null;
  updated_at?: number | null;
}

interface NativeAuthResponse {
  providers: NativeAuthStatus[];
}

export async function getNativeAuthStatuses(): Promise<NativeAuthStatus[]> {
  const { data } = await api.get<NativeAuthResponse>('/api/v1/native-agents/auth');
  return data.providers;
}

export async function startNativeLogin(provider: string): Promise<NativeLoginSession> {
  const { data } = await api.post<NativeLoginSession>(
    `/api/v1/native-agents/${encodeURIComponent(provider)}/login`,
  );
  return data;
}

export async function getNativeLogin(sessionId: string): Promise<NativeLoginSession> {
  const { data } = await api.get<NativeLoginSession>(
    `/api/v1/native-agents/login/${encodeURIComponent(sessionId)}`,
  );
  return data;
}

export async function submitNativeLoginCode(
  sessionId: string,
  code: string,
): Promise<NativeLoginSession> {
  const { data } = await api.post<NativeLoginSession>(
    `/api/v1/native-agents/login/${encodeURIComponent(sessionId)}/input`,
    { code },
  );
  return data;
}

export async function cancelNativeLogin(sessionId: string): Promise<NativeLoginSession> {
  const { data } = await api.delete<NativeLoginSession>(
    `/api/v1/native-agents/login/${encodeURIComponent(sessionId)}`,
  );
  return data;
}
