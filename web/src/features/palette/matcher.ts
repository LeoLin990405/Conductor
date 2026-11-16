/**
 * D1: case-insensitive fuzzy subsequence matcher for palette commands.
 * Needle and haystacks are lower-cased; one leading / is stripped; rank is
 * exact > prefix > lower gap cost > earlier start > canonical name.
 */
import {
  COMMANDS,
  COMPLETION_LIMIT,
  idleCommands,
  type PaletteCommand,
} from './commands';

export type FuzzyScore = {
  idx: number[];
  exact: number;
  prefix: number;
  gap: number;
  start: number;
};

export type RankedCommand = {
  command: PaletteCommand;
  score: FuzzyScore | null;
  /** Haystack that produced the best score (canonical, slash body, or alias). */
  key: string;
  onName: boolean;
};

function fold(s: string): string {
  return s.toLowerCase();
}

export function haystacks(cmd: PaletteCommand): string[] {
  const out = [cmd.name];
  if (cmd.slash) out.push(cmd.slash.replace(/^\//, ''));
  for (const alias of cmd.aliases) {
    out.push(alias.replace(/^\//, ''));
  }
  return out;
}

/** Greedy leftmost subsequence match; null when the needle is not a subsequence. */
export function fuzzyMatch(needle: string, hay: string): FuzzyScore | null {
  const q = fold(needle);
  const h = fold(hay);
  if (!q) {
    return { idx: [], exact: h === '' ? 0 : 1, prefix: 0, gap: 0, start: 0 };
  }
  const idx: number[] = [];
  let i = 0;
  for (let j = 0; j < h.length && i < q.length; j++) {
    if (h.charAt(j) === q.charAt(i)) {
      idx.push(j);
      i++;
    }
  }
  if (i < q.length) return null;
  return {
    idx,
    exact: h === q ? 0 : 1,
    prefix: h.startsWith(q) ? 0 : 1,
    gap: idx[idx.length - 1]! - idx[0]! - (q.length - 1),
    start: idx[0]!,
  };
}

function cmpScore(a: FuzzyScore, b: FuzzyScore): number {
  for (const key of ['exact', 'prefix', 'gap', 'start'] as const) {
    if (a[key] !== b[key]) return a[key] < b[key] ? -1 : 1;
  }
  return 0;
}

export function bestMatch(
  query: string,
  cmd: PaletteCommand,
): { score: FuzzyScore; key: string } | null {
  let best: FuzzyScore | null = null;
  let key: string | null = null;
  for (const hs of haystacks(cmd)) {
    const m = fuzzyMatch(query, hs);
    if (m && (!best || cmpScore(m, best) < 0)) {
      best = m;
      key = hs;
    }
  }
  return best && key !== null ? { score: best, key } : null;
}

/** Rank commands for a query; empty needle returns idleOrder unchanged. */
export function rankCommands(
  rawQuery: string,
  commands: readonly PaletteCommand[] = COMMANDS,
  limit: number = COMPLETION_LIMIT,
): RankedCommand[] {
  const needle = fold(rawQuery.replace(/^\//, ''));
  if (!needle) {
    const idle =
      commands === COMMANDS
        ? idleCommands()
        : [...commands];
    return idle.slice(0, limit).map((command) => ({
      command,
      score: null,
      key: command.name,
      onName: true,
    }));
  }

  const out: RankedCommand[] = [];
  for (const command of commands) {
    const hit = bestMatch(needle, command);
    if (!hit) continue;
    out.push({
      command,
      score: hit.score,
      key: hit.key,
      onName: hit.key === command.name,
    });
  }
  out.sort(
    (a, b) =>
      cmpScore(a.score!, b.score!) ||
      (a.command.name < b.command.name ? -1 : a.command.name > b.command.name ? 1 : 0),
  );
  return out.slice(0, limit);
}
