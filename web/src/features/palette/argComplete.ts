/**
 * D2: after the command token + space, unique case-sensitive id prefixes.
 * dispatch lists every active id; approve/reject only awaiting_human;
 * reply offers nothing (free text).
 */
import { listActiveDispatches } from '@/api/dispatch';
import { COMPLETION_LIMIT } from './commands';

export type DispatchIdRow = {
  id: string;
  phase?: string;
  state?: string;
  hint?: string;
};

export type ArgOption = {
  id: string;
  hint: string;
  phase?: string;
};

export type ArgCompleteResult = {
  options: ArgOption[];
  /** Form still needs an argument and nothing is offered. */
  needsId: boolean;
  needHintEn: string | null;
  needHintZh: string | null;
  freeText: boolean;
};

type ListActive = typeof listActiveDispatches;

function phaseOf(row: DispatchIdRow & Record<string, unknown>): string {
  if (typeof row.phase === 'string' && row.phase) return row.phase;
  if (typeof row.state === 'string' && row.state) return row.state;
  return '';
}

function asRows(data: unknown): DispatchIdRow[] {
  if (!Array.isArray(data)) return [];
  const out: DispatchIdRow[] = [];
  for (const item of data) {
    if (!item || typeof item !== 'object') continue;
    const row = item as DispatchIdRow & Record<string, unknown>;
    const id = typeof row.id === 'string' ? row.id : '';
    if (!id) continue;
    out.push({
      id,
      phase: phaseOf(row),
      state: typeof row.state === 'string' ? row.state : undefined,
      hint: typeof row.hint === 'string' ? row.hint : undefined,
    });
  }
  return out;
}

/** Case-sensitive prefix filter; never fuzzy. */
export function prefixMatch(partial: string, opts: ArgOption[]): ArgOption[] {
  if (!partial) return opts;
  return opts.filter((o) => o.id.startsWith(partial));
}

/**
 * Unique prefix insert: when exactly one option matches, return its id;
 * ambiguous or empty → null (list stays; insert nothing).
 */
export function uniquePrefixInsert(partial: string, opts: ArgOption[]): string | null {
  const hits = prefixMatch(partial, opts);
  return hits.length === 1 ? hits[0]!.id : null;
}

async function loadDispatchIds(listActive: ListActive): Promise<DispatchIdRow[]> {
  const data = await listActive();
  return asRows(data);
}

function toOptions(rows: DispatchIdRow[]): ArgOption[] {
  return rows.map((r) => ({
    id: r.id,
    hint: r.hint || (r.phase ? r.phase : ''),
    phase: r.phase,
  }));
}

/**
 * Argument completions for a parsed command + partial token.
 * `listActive` is injectable so unit tests can mock without the network.
 */
export async function completeArgs(
  command: string,
  partial: string,
  listActive: ListActive = listActiveDispatches,
): Promise<ArgCompleteResult> {
  const empty: ArgCompleteResult = {
    options: [],
    needsId: false,
    needHintEn: null,
    needHintZh: null,
    freeText: false,
  };

  if (command === 'reply') {
    return {
      ...empty,
      freeText: true,
      needsId: !partial,
      needHintEn: 'reply needs text',
      needHintZh: 'reply 需要回复内容',
    };
  }

  if (command !== 'dispatch' && command !== 'approve' && command !== 'reject') {
    return empty;
  }

  const rows = await loadDispatchIds(listActive);
  const filtered =
    command === 'dispatch'
      ? rows
      : rows.filter((r) => phaseOf(r) === 'awaiting_human');
  const options = prefixMatch(partial, toOptions(filtered)).slice(0, COMPLETION_LIMIT);
  const needsId = (command === 'approve' || command === 'reject') && options.length === 0;
  return {
    options,
    needsId,
    needHintEn: needsId ? `${command} needs a dispatch id` : null,
    needHintZh: needsId ? `${command} 需要一个 dispatch id` : null,
    freeText: false,
  };
}
