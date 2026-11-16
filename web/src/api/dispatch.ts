/** Dispatch supervision HTTP client. */
import { ApiError, api } from '@/api/client';
import type { DispatchSummary, ProviderCatalog } from '@/features/dispatch/model';

function statusOf(err: unknown): number | null {
  if (typeof err !== 'object' || err === null) return null;
  const row = err as { status?: unknown; response?: { status?: unknown } };
  const status = row.response?.status ?? row.status;
  return typeof status === 'number' ? status : null;
}

function rowsOf(data: unknown): DispatchSummary[] {
  if (Array.isArray(data)) return data.filter((row) => row && typeof row === 'object');
  if (!data || typeof data !== 'object') return [];
  const body = data as { dispatches?: unknown; active?: unknown };
  const rows = Array.isArray(body.dispatches) ? body.dispatches : body.active;
  return Array.isArray(rows) ? rows.filter((row) => row && typeof row === 'object') : [];
}

export async function listActiveDispatches(): Promise<DispatchSummary[]> {
  const { data } = await api.get('/api/v1/dispatch/status/active');
  return rowsOf(data);
}

export async function getDispatch(id: string): Promise<DispatchSummary | null> {
  try {
    const { data } = await api.get<DispatchSummary>(`/api/v1/dispatch/${encodeURIComponent(id)}`);
    return data;
  } catch (err) {
    if (err instanceof ApiError && statusOf(err) === 404) return null;
    if (statusOf(err) === 404) return null;
    throw err;
  }
}

/** Start a dispatch for a goal. The backend plans it before any line runs. */
export async function startDispatch(goal: string): Promise<DispatchSummary> {
  const { data } = await api.post<DispatchSummary>('/api/v1/dispatch', { goal });
  return data;
}

export async function approveDispatch(id: string, note: string): Promise<void> {
  await api.post(`/api/v1/dispatch/${encodeURIComponent(id)}/approve`, { note });
}

export async function rejectDispatch(id: string, note: string): Promise<void> {
  await api.post(`/api/v1/dispatch/${encodeURIComponent(id)}/reject`, { note });
}

/** Override a blocked line's model and drive it from its next attempt. */
export async function reassignDispatchLine(
  id: string,
  lineId: string,
  model: string,
): Promise<DispatchSummary> {
  const { data } = await api.post<DispatchSummary>(
    `/api/v1/dispatch/${encodeURIComponent(id)}/lines/${encodeURIComponent(lineId)}/reassign`,
    { model },
  );
  return data;
}

/**
 * Which provider serves which model.
 *
 * A line names its model, never its provider, so the page asks the model
 * catalog. The catalog is the authority: a model it does not name has no
 * provider here, rather than a guessed one.
 */
export async function getProviderCatalog(): Promise<ProviderCatalog> {
  const { data } = await api.get('/api/v1/models');
  const body = (data ?? {}) as {
    models?: Record<string, { models?: unknown }>;
    model_metadata?: Record<string, { provider?: unknown }>;
  };
  const byModel: Record<string, string> = {};
  const providers: string[] = [];
  for (const [provider, group] of Object.entries(body.models ?? {})) {
    if (!providers.includes(provider)) providers.push(provider);
    const models = Array.isArray(group?.models) ? group.models : [];
    for (const model of models) {
      const key = String(model ?? '').trim();
      if (key && !byModel[key]) byModel[key] = provider;
    }
  }
  for (const [model, entry] of Object.entries(body.model_metadata ?? {})) {
    const provider = typeof entry?.provider === 'string' ? entry.provider.trim() : '';
    if (!provider) continue;
    if (!providers.includes(provider)) providers.push(provider);
    if (!byModel[model]) byModel[model] = provider;
  }
  return { byModel, providers };
}
