/**
 * Read-only fleet skills inventory.
 *
 * Mirrors GET /api/v1/fleet-skills: per-agent skill listings (which entries
 * Conductor manages, which symlinks are broken) plus the central repo
 * contents, presets and per-project sets. There is intentionally no write
 * API here — installs and uninstalls happen through `conductor skills`.
 */
import { api } from '@/api/client';
import { apiErrorDetail } from '@/api/core/api/errors';

export interface FleetSkillEntry {
  name: string;
  kind: 'symlink' | 'copy';
  managed: boolean;
  healthy: boolean;
}

export interface FleetAgentSkills {
  dir: string;
  exists: boolean;
  entries: FleetSkillEntry[];
  foreign: string[];
  broken_symlinks: string[];
  /** Set when this agent's directory could not be read; the rest of the
   *  inventory is still valid. */
  error?: string;
}

export interface FleetRepoSkill {
  name: string;
  description: string;
  installed: string[];
}

export interface FleetPreset {
  name: string;
  skills: string[];
  description: string;
}

export interface FleetProject {
  project: string;
  lines: string[];
}

export interface FleetSkillsInventory {
  repo: string;
  skills: FleetRepoSkill[];
  agents: Record<string, FleetAgentSkills>;
  presets: FleetPreset[];
  projects: FleetProject[];
}

export async function getFleetSkillsInventory(agents?: string[]): Promise<FleetSkillsInventory> {
  const params =
    agents && agents.length > 0 ? new URLSearchParams(agents.map((agent) => ['agent', agent])) : undefined;
  const { data } = await api.get<FleetSkillsInventory>('/api/v1/fleet-skills', { params });
  return data;
}

/**
 * What a fleet-skills failure should say. FastAPI answers a refused request
 * with a plain-string `detail`; the shared client's `ApiError` message keeps
 * axios's "Request failed with status code N" in that case, so the server's
 * sentence is preferred whenever one came back.
 */
export function fleetSkillsErrorMessage(error: unknown): string {
  const detail = apiErrorDetail(error);
  if (typeof detail === 'string' && detail) return detail;
  return error instanceof Error ? error.message : String(error);
}
