/**
 * Workspace management endpoints.
 */
import { api } from '@/api/client';
import type {
  Workspace,
  WorkspaceCreate,
  WorkspacesResponse,
} from '@/types/api';

// The shared API client sets no global timeout. Workspace-management ops can
// legitimately run tens of seconds, so this bound is generous — it converts a
// network hang into a visible failure rather than race the server.
const WORKSPACE_MUTATION_TIMEOUT_MS = 120000;

export async function getWorkspaces(limit: number = 20, offset: number = 0, sortBy: string = 'custom', includeHome: boolean = false): Promise<WorkspacesResponse> {
  const { data } = await api.get<WorkspacesResponse>('/api/v1/workspaces', {
    params: { limit, offset, sort_by: sortBy, ...(includeHome ? { include_home: true } : {}) },
  });
  return data;
}

/** `computerId` puts the folder on that computer; without it the server uses the primary. */
export async function createWorkspace(
  name: string,
  description: string = '',
  config: Record<string, unknown> = {},
  computerId?: string | null,
): Promise<Workspace> {
  const body: WorkspaceCreate = { name, description, config, ...(computerId ? { computer_id: computerId } : {}) };
  const { data } = await api.post<Workspace>('/api/v1/workspaces', body);
  return data;
}

export async function deleteWorkspace(workspaceId: string) {
  if (!workspaceId) throw new Error('Workspace ID is required');
  const id = String(workspaceId).trim();
  if (!id) throw new Error('Workspace ID cannot be empty');
  await api.delete(`/api/v1/workspaces/${id}`);
}

export async function getWorkspace(workspaceId: string): Promise<Workspace> {
  if (!workspaceId) throw new Error('Workspace ID is required');
  const { data } = await api.get<Workspace>(`/api/v1/workspaces/${workspaceId}`);
  return data;
}

/**
 * Get or create the user's Home, the workspace All workspaces runs in.
 * Idempotent, so safe to call on every app load.
 */
export async function getHomeWorkspace(): Promise<Workspace> {
  const { data } = await api.post<Workspace>('/api/v1/workspaces/home');
  return data;
}

export async function updateWorkspace(workspaceId: string, updates: Record<string, unknown>) {
  if (!workspaceId) throw new Error('Workspace ID is required');
  const { data } = await api.put(`/api/v1/workspaces/${workspaceId}`, updates);
  return data;
}

export async function reorderWorkspaces(items: Array<{ workspace_id: string; sort_order: number }>) {
  if (!items?.length) throw new Error('Reorder items are required');
  await api.post('/api/v1/workspaces/reorder', { items });
}

/**
 * Rename a workspace. Thin wrapper over the existing update endpoint
 * (PUT /api/v1/workspaces/{id} with { name }).
 */
export async function renameWorkspace(workspaceId: string, name: string) {
  if (!workspaceId) throw new Error('Workspace ID is required');
  const { data } = await api.put(`/api/v1/workspaces/${workspaceId}`, { name }, {
    timeout: WORKSPACE_MUTATION_TIMEOUT_MS,
  });
  return data;
}
