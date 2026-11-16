import { api } from '@/api/client';

export interface LocalVersionInfo {
  supported: boolean;
  enabled: boolean;
  version: string;
  commit?: string;
  branch?: string;
  dirty?: boolean;
  remote?: string;
  reason?: string;
}

export interface LocalUpdateCheck {
  update_available: boolean;
  behind: number;
  ahead: number;
  current: string;
  remote: string;
  commits: string[];
}

export interface LocalUpdateApply {
  updated: boolean;
  current?: string;
  old_commit?: string;
  new_commit?: string;
  restart_required?: boolean;
  message: string;
}

export async function getLocalVersion(): Promise<LocalVersionInfo> {
  const { data } = await api.get('/api/v1/system/version');
  return data;
}

export async function checkLocalUpdate(): Promise<LocalUpdateCheck> {
  const { data } = await api.post('/api/v1/system/update/check');
  return data;
}

export async function applyLocalUpdate(): Promise<LocalUpdateApply> {
  const { data } = await api.post('/api/v1/system/update/apply');
  return data;
}

export async function restartLocalUpdate(): Promise<{ restarting: boolean }> {
  const { data } = await api.post('/api/v1/system/update/restart');
  return data;
}
