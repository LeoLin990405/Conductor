/**
 * Runtime readiness — `GET /api/v1/system/readiness`.
 *
 * The answer describes the signed-in user's own configuration and is served
 * `Cache-Control: private, no-store`, so it is asked for through the shared API
 * client (which supplies the Bearer token and base URL) and never persisted
 * outside React Query. There is deliberately no transport of its own here.
 */
import { api } from '@/api/client';

/** The server's overall verdict over every check. */
export type ReadinessStatus = 'ready' | 'degraded' | 'needs_action';

/** One check's own result. `unknown` is a definite non-answer, not a failure. */
export type ReadinessCheckStatus = 'ok' | 'missing' | 'degraded' | 'unknown';

/**
 * How much the check itself established: `observed` means it actually looked,
 * `configuration_only` means it only found configuration/credentials present
 * (validity and reachability are NOT established), `not_checked` means it did
 * not run.
 */
export type ReadinessVerification = 'observed' | 'configuration_only' | 'not_checked';

export interface ReadinessCheck {
  id: string;
  status: ReadinessCheckStatus;
  required: boolean;
  summary: string;
  verification: ReadinessVerification;
}

export interface ReadinessResponse {
  status: ReadinessStatus;
  checked_at: string;
  verification: 'local_checks_only';
  checks: ReadinessCheck[];
}

export async function getReadiness(): Promise<ReadinessResponse> {
  const { data } = await api.get<ReadinessResponse>('/api/v1/system/readiness');
  return data;
}
