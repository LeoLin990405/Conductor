/**
 * Client for the Skills & MCP matrices.
 *
 * Fleet skills (`/api/v1/fleet-skills`) is a different, read-only inventory.
 * This hub has no route in the server, so the live page tries
 * `/api/v1/skills-mcp/status` and a 404 means the local CLI is the source of
 * truth. Callers pass a fetcher in tests; the default is the only network path.
 */

export type CellState = 'installed' | 'missing' | 'drifted' | 'conflict';

export type MatrixKind = 'skills' | 'mcp';

export type ActionName = 'adopt' | 'apply' | 'rollback';

export interface MatrixCell {
  name: string;
  state: CellState;
}

export interface AgentRow {
  name: string;
  cells: MatrixCell[];
  env?: Record<string, unknown>;
}

export interface SkillMatrix {
  agents: AgentRow[];
}

export interface SkillsMcpStatus {
  skills: SkillMatrix;
  mcp: SkillMatrix;
}

export interface ActionRequest {
  kind: MatrixKind;
  agent: string;
}

export const REDACTED = '••••';

export const SKILLS_STATUS_COMMAND = 'conductor skills status --json';

const SECRET_FIELD = /token|secret|password|api_key/i;

const CELL_STATES: ReadonlySet<string> = new Set(['installed', 'missing', 'drifted', 'conflict']);

const GLYPH: Record<CellState, string> = {
  installed: '✔',
  missing: '✖',
  drifted: '◆',
  conflict: '◆',
};

export class SkillsMcpMissing extends Error {
  readonly status = 404;

  constructor() {
    super(`The local CLI is the source of truth: ${SKILLS_STATUS_COMMAND}`);
    this.name = 'SkillsMcpMissing';
  }
}

export function isSecretField(key: string): boolean {
  return SECRET_FIELD.test(key);
}

export function cellText(state: CellState): string {
  return `${GLYPH[state]} ${state}`;
}

export function matrixColumns(matrix: SkillMatrix): string[] {
  const columns: string[] = [];
  const seen = new Set<string>();
  for (const agent of matrix.agents) {
    for (const cell of agent.cells) {
      if (seen.has(cell.name)) continue;
      seen.add(cell.name);
      columns.push(cell.name);
    }
  }
  return columns;
}

export function cellAt(agent: AgentRow, name: string): MatrixCell | undefined {
  return agent.cells.find((cell) => cell.name === name);
}

function isCellState(value: unknown): value is CellState {
  return typeof value === 'string' && CELL_STATES.has(value);
}

function normalizeCells(input: unknown): MatrixCell[] {
  if (!Array.isArray(input)) return [];
  const cells: MatrixCell[] = [];
  for (const item of input) {
    if (!item || typeof item !== 'object') continue;
    const cell = item as Record<string, unknown>;
    const name = typeof cell.name === 'string' ? cell.name : '';
    if (!name || !isCellState(cell.state)) continue;
    cells.push({ name, state: cell.state });
  }
  return cells;
}

function normalizeAgent(input: unknown): AgentRow | null {
  if (!input || typeof input !== 'object') return null;
  const row = input as Record<string, unknown>;
  const name = typeof row.name === 'string' ? row.name : typeof row.id === 'string' ? row.id : '';
  if (!name) return null;
  const env =
    row.env && typeof row.env === 'object' && !Array.isArray(row.env)
      ? { ...(row.env as Record<string, unknown>) }
      : undefined;
  return {
    name,
    cells: normalizeCells(row.cells),
    ...(env ? { env } : {}),
  };
}

function normalizeMatrix(input: unknown): SkillMatrix {
  const body = input && typeof input === 'object' ? (input as Record<string, unknown>) : {};
  const agents = Array.isArray(body.agents)
    ? body.agents.map(normalizeAgent).filter((row): row is AgentRow => row !== null)
    : [];
  return { agents };
}

export function normalizeStatus(input: unknown): SkillsMcpStatus {
  const body = input && typeof input === 'object' ? (input as Record<string, unknown>) : {};
  return {
    skills: normalizeMatrix(body.skills),
    mcp: normalizeMatrix(body.mcp),
  };
}

function gatherSecretStrings(value: unknown, secrets: Set<string>): void {
  if (typeof value === 'string') {
    if (value.length >= 4) secrets.add(value);
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) gatherSecretStrings(item, secrets);
    return;
  }
  if (value && typeof value === 'object') {
    for (const child of Object.values(value as Record<string, unknown>)) {
      gatherSecretStrings(child, secrets);
    }
  }
}

function walkSecrets(value: unknown, key: string | undefined, secrets: Set<string>): void {
  if (key && isSecretField(key)) {
    gatherSecretStrings(value, secrets);
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) walkSecrets(item, undefined, secrets);
    return;
  }
  if (value && typeof value === 'object') {
    for (const [childKey, child] of Object.entries(value as Record<string, unknown>)) {
      walkSecrets(child, childKey, secrets);
    }
  }
}

export function collectSecretSet(value: unknown): Set<string> {
  const secrets = new Set<string>();
  walkSecrets(value, undefined, secrets);
  return secrets;
}

/** A diff can repeat a secret field's value without using that field's name. */
export function scrubSecrets(text: string, secrets: ReadonlySet<string>): string {
  let next = text;
  const ordered = [...secrets].filter((secret) => secret.length >= 4).sort((a, b) => b.length - a.length);
  for (const secret of ordered) {
    if (next.includes(secret)) next = next.split(secret).join(REDACTED);
  }
  return next;
}

function rewriteValue(value: unknown, secrets: ReadonlySet<string>): unknown {
  if (typeof value === 'string') return scrubSecrets(value, secrets);
  if (typeof value === 'number' || typeof value === 'boolean' || value == null) return value;
  if (Array.isArray(value)) return value.map((item) => rewriteValue(item, secrets));
  if (typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      out[key] = isSecretField(key) ? REDACTED : rewriteValue(child, secrets);
    }
    return out;
  }
  return value;
}

function redactMatrix(matrix: SkillMatrix, secrets: ReadonlySet<string>): SkillMatrix {
  return {
    agents: matrix.agents.map((agent) => ({
      name: scrubSecrets(agent.name, secrets),
      cells: agent.cells.map((cell) => ({
        name: scrubSecrets(cell.name, secrets),
        state: cell.state,
      })),
      ...(agent.env ? { env: rewriteValue(agent.env, secrets) as Record<string, unknown> } : {}),
    })),
  };
}

export function redactStatus(input: unknown): SkillsMcpStatus {
  const normalized = normalizeStatus(input);
  const secrets = collectSecretSet(normalized);
  return {
    skills: redactMatrix(normalized.skills, secrets),
    mcp: redactMatrix(normalized.mcp, secrets),
  };
}

export function envEntries(env: Record<string, unknown> | undefined): Array<{ key: string; value: string }> {
  if (!env) return [];
  return Object.entries(env).map(([key, value]) => ({
    key,
    value: isSecretField(key) ? REDACTED : presented(value),
  }));
}

function presented(value: unknown): string {
  const safe = rewriteValue(value, new Set());
  if (typeof safe === 'string') return safe;
  if (typeof safe === 'number' || typeof safe === 'boolean') return String(safe);
  if (safe == null) return '';
  return JSON.stringify(safe);
}

export async function fetchSkillsMcpStatus(): Promise<SkillsMcpStatus> {
  let response: Response;
  try {
    response = await fetch('/api/v1/skills-mcp/status');
  } catch (error) {
    const message = error instanceof Error ? error.message : 'network error';
    throw new Error(`Skills & MCP status failed (${message})`, { cause: error });
  }
  if (response.status === 404) throw new SkillsMcpMissing();
  if (!response.ok) throw new Error(`Skills & MCP status failed (${response.status})`);
  try {
    return normalizeStatus(await response.json());
  } catch (error) {
    throw new Error('Skills & MCP status failed (invalid JSON)', { cause: error });
  }
}

export async function loadSkillsMcp(
  fetcher: (() => Promise<SkillsMcpStatus>) | undefined = fetchSkillsMcpStatus,
): Promise<SkillsMcpStatus> {
  return redactStatus(await fetcher());
}

function isDiffBody(value: unknown): value is { diff: string } {
  return Boolean(value) && typeof value === 'object' && typeof (value as { diff?: unknown }).diff === 'string';
}

export async function readActionResult(response: Response): Promise<string> {
  const text = await response.text();
  let parsed: unknown = text;
  if (text) {
    try {
      parsed = JSON.parse(text) as unknown;
    } catch {
      parsed = text;
    }
  }
  const secrets = collectSecretSet(parsed);
  if (!response.ok) return scrubSecrets(`Request failed (${response.status})`, secrets);
  if (isDiffBody(parsed)) return scrubSecrets(parsed.diff, secrets);
  if (typeof parsed === 'string') return scrubSecrets(parsed, secrets);
  return JSON.stringify(rewriteValue(parsed, secrets), null, 2);
}

export async function postSkillsMcpAction(action: ActionName, request: ActionRequest): Promise<string> {
  const response = await fetch(`/api/v1/skills-mcp/${action}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(request),
  });
  return readActionResult(response);
}

export async function settleAction(action: () => Promise<string>): Promise<string> {
  try {
    return await action();
  } catch (error) {
    return error instanceof Error ? error.message : 'Action failed';
  }
}
