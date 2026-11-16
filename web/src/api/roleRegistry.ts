/** Role registry HTTP client (R6 L1): four roles, candidates, defaults, constraints. */
import { api } from '@/api/client';

export type RoleName = 'router' | 'planner' | 'reviewer' | 'worker';

export const ROLE_NAMES: readonly RoleName[] = ['router', 'planner', 'reviewer', 'worker'];

export interface RoleConstraints {
  read_only: boolean;
  may_write: boolean;
  may_execute: boolean;
  requires_session_resume: boolean;
  excluded: string[];
  max_parallel: number | null;
}

export interface RoleEntry {
  role: RoleName;
  candidates: string[];
  default: string | null;
  constraints: RoleConstraints;
  extra: Record<string, unknown>;
}

export interface RoleRegistryView {
  version: number;
  source: 'store' | 'fallback';
  revision: number;
  hard_rules: string[];
  roles: Record<RoleName, RoleEntry>;
}

export interface RoleUpdate {
  candidates?: string[];
  default?: string;
  constraints?: Partial<RoleConstraints>;
  extra?: Record<string, unknown>;
  reason: string;
  expected_revision?: number;
}

export interface RoleAuditRow {
  audit_id: number;
  revision: number;
  role: RoleName | '*';
  actor: string;
  reason: string;
  before: Partial<RoleEntry> | null;
  after: Partial<RoleEntry>;
  created_at?: string;
}

export async function getRoleRegistry(): Promise<RoleRegistryView> {
  const { data } = await api.get<RoleRegistryView>('/api/v1/roles');
  return data;
}

export async function updateRole(role: RoleName, body: RoleUpdate): Promise<RoleRegistryView> {
  const { data } = await api.put<RoleRegistryView>(
    `/api/v1/roles/${encodeURIComponent(role)}`,
    body,
  );
  return data;
}

export async function getRoleAudit(limit = 20): Promise<RoleAuditRow[]> {
  const { data } = await api.get<{ audit: RoleAuditRow[] }>('/api/v1/roles/audit', {
    params: { limit },
  });
  return Array.isArray(data?.audit) ? data.audit : [];
}
