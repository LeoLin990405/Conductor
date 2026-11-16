/** Shared dispatch view rules. Outer gate fields only. */
import { registerFeatureLocales } from '@/lib/featureLocales';
import { userLocalStorage } from '@/lib/userStorage';
import en from './locales/tnDispatch.en-US.json';
import zh from './locales/tnDispatch.zh-CN.json';

registerFeatureLocales({ 'en-US': en, 'zh-CN': zh });

export interface DispatchLine {
  id?: string;
  name?: string;
  task?: string;
  status?: string;
  assigned_model?: string;
  branch?: string;
  base_commit?: string;
  attempts?: number;
  depends_on?: string[];
  write_scope?: string[];
  acceptance?: unknown[];
  failure_reason?: string | null;
  blockers?: unknown[];
  risk_bearing?: boolean;
}

export interface GateCheck {
  result?: string;
  reason?: string;
  verdict?: string;
  reviewer?: string;
  round?: number;
  blocker_count?: number;
  /** The revision a gate pinned (G2 records the commit its tests ran on). */
  revision?: string;
  at?: string;
}

/** One review round, as the backend's review history keeps it. */
export interface ReviewRecord {
  round?: number;
  gate?: string;
  result?: string;
  reason?: string;
  verdict?: string;
  reviewer?: string;
  blocker_count?: number;
  at?: string;
}

/**
 * The findings of the last review that rejected the branch tip.
 *
 * The head it saw is the revision the review judged, which is the only
 * reviewed commit the backend records.
 */
export interface LastReviewBlockers {
  head?: string;
  branch?: string;
  reviewer?: string;
  count?: number;
  blockers?: string[];
  at?: string;
}

export interface GateRecord {
  phase?: string;
  fix_round?: number;
  risk_bearing?: boolean;
  return_stage?: string;
  blocking_reviewer?: string;
  blocking_rounds?: number;
  author?: string;
  primary_reviewer?: string;
  secondary_reviewer?: string;
  reason?: string;
  updated_at?: string;
  gates?: Record<string, GateCheck>;
  history?: ReviewRecord[];
  /** The head the last rejecting review saw, and what it found. */
  last_review_blockers?: LastReviewBlockers;
  events?: Array<{
    from_stage?: string;
    to_stage?: string;
    gate?: string;
    result?: string;
    reason?: string;
    timestamp?: string;
  }>;
  /** `stage`, `return_stage` and the reviewers are read. The rest of `machine` stays off the page. */
  machine?: {
    stage?: string;
    return_stage?: string;
    author_provider?: string | null;
    primary_reviewer?: string | null;
    secondary_reviewer?: string | null;
  };
}

export interface DispatchSummary {
  id?: string;
  goal?: string;
  state?: string;
  progress_pct?: number;
  integrate_branch?: string;
  message?: string;
  created_at?: string;
  fake?: boolean;
  approval_token?: string;
  spec_ref?: string;
  lines?: DispatchLine[];
  /** Human reassigns waiting for the running drive, and the ones it dropped. */
  human_reassigns?: {
    queued?: Array<{ line_id?: string; kind?: string; model?: string | null }>;
    dropped?: Array<{ line_id?: string; model?: string | null; reason?: string | null }>;
  };
  gates?: {
    lines?: Record<string, GateRecord>;
    approval?: { token_present?: boolean };
  };
}

export const TERMINAL_STATES = new Set(['COMPLETE', 'ABORT', 'ERROR']);

function withoutControls(value: string): string {
  let cleaned = '';
  for (const char of value) {
    const code = char.charCodeAt(0);
    const control = code <= 8 || code === 11 || code === 12 || (code >= 14 && code <= 31) || (code >= 127 && code <= 159);
    if (!control) cleaned += char;
  }
  return cleaned;
}

export function clip(value: unknown, limit: number): string {
  const text = withoutControls(String(value ?? '').split('\n')[0]).trim();
  if (text.length <= limit) return text;
  return `${text.slice(0, limit - 1)}…`;
}

export function lineId(row: DispatchLine): string {
  return String(row.id || row.name || '');
}

export function dependsOn(row: DispatchLine): string[] {
  if (!Array.isArray(row.depends_on)) return [];
  return row.depends_on.map((item) => String(item)).filter((item) => item.trim());
}

export function dagLayers(rows: DispatchLine[] | undefined): { layers: string[][]; warnings: string[] } {
  const order: string[] = [];
  const deps: Record<string, string[]> = {};
  for (const row of rows ?? []) {
    const id = lineId(row);
    if (!id || id in deps) continue;
    order.push(id);
    deps[id] = dependsOn(row);
  }
  const known = new Set(order);
  const warnings: string[] = [];
  for (const id of order) {
    const missing = deps[id].filter((item) => !known.has(item));
    if (missing.length) warnings.push(`⚠ missing dep ${id} ← ${missing.join(', ')}`);
  }
  const remaining = [...order];
  const layers: string[][] = [];
  while (remaining.length) {
    const ready = remaining.filter((id) =>
      deps[id].every((item) => !known.has(item) || !remaining.includes(item)),
    );
    if (!ready.length) {
      warnings.push(`⚠ cycle ${remaining.join(', ')}`);
      layers.push([...remaining]);
      break;
    }
    layers.push(ready);
    const placed = new Set(ready);
    for (let index = remaining.length - 1; index >= 0; index -= 1) {
      if (placed.has(remaining[index])) remaining.splice(index, 1);
    }
  }
  return { layers, warnings };
}

export function gateRecord(summary: DispatchSummary, id: string): GateRecord | null {
  const bucket = summary.gates?.lines;
  if (!bucket || !summary.gates || Object.keys(summary.gates).length === 0) return null;
  const row = bucket[id];
  return row && typeof row === 'object' ? row : null;
}

export function verdictLabel(value: unknown): string {
  const text = clip(value, 40);
  if (!text) return '—';
  const lowered = text.toLowerCase();
  if (lowered === 'merge') return 'MERGE';
  if (lowered === 'block') return 'BLOCK';
  return text;
}

/** The verdict of one line, in words. The line's verdict, not a round's. */
export function verdictOf(record: GateRecord | null): string {
  const mark = lineVerdict(record);
  if (mark.id === 'merge' || mark.id === 'approved') return 'MERGE';
  if (mark.id === 'block') return 'BLOCK';
  return '—';
}

export function tokenIssued(summary: DispatchSummary): boolean {
  if (summary.approval_token) return true;
  return Boolean(summary.gates?.approval?.token_present);
}

export function unassignedReason(row: DispatchLine): string {
  if (row.failure_reason) return clip(row.failure_reason, 120);
  const blocker = row.blockers?.find((item) => item != null && String(item).trim());
  return blocker ? clip(blocker, 120) : '—';
}

/* ------------------------------------------------------------------ *
 * Marks: a glyph and a word, never a glyph alone.
 *
 * The glyphs are the terminal-native status set (tokens.json `status`) and
 * the review verdicts of the dispatch design. The word is a locale key, so
 * the page prints the pair in the reader's language.
 * ------------------------------------------------------------------ */

export type StatusId =
  | 'queued'
  | 'running'
  | 'blocked'
  | 'needs-you'
  | 'failed'
  | 'completed'
  | 'archived';

export type Tone = 'muted' | 'tempo' | 'ok' | 'hold' | 'error';

export const STATUS_GLYPHS: Record<StatusId, string> = {
  queued: '○',
  running: '●',
  blocked: '▣',
  'needs-you': '◆',
  failed: '✖',
  completed: '✔',
  archived: '⊘',
};

export const STATUS_TONES: Record<StatusId, Tone> = {
  queued: 'muted',
  running: 'tempo',
  blocked: 'hold',
  'needs-you': 'hold',
  failed: 'error',
  completed: 'ok',
  archived: 'muted',
};

export interface StatusMark {
  id: StatusId;
  glyph: string;
  tone: Tone;
}

/** The dispatch states the backend reports. */
const STATE_STATUS: Record<string, StatusId> = {
  SUBMITTED: 'queued',
  PLANNING: 'queued',
  ASSIGNED: 'queued',
  LAUNCHING: 'queued',
  MONITORING: 'running',
  REVIEW: 'running',
  APPROVAL: 'needs-you',
  MERGE: 'running',
  COMPLETE: 'completed',
  ABORT: 'archived',
  ERROR: 'failed',
};

/** The gate phases a line walks through. */
const PHASE_STATUS: Record<string, StatusId> = {
  planning: 'queued',
  spec_review: 'queued',
  dag_ready: 'queued',
  assigned: 'queued',
  implementing: 'running',
  focused_tests: 'running',
  g2: 'running',
  primary_review: 'running',
  secondary_review: 'running',
  integration_checks: 'running',
  g5: 'running',
  awaiting_human: 'needs-you',
  approval: 'needs-you',
  g6: 'needs-you',
  merged: 'completed',
  fixing: 'blocked',
  blocked: 'blocked',
  replanning: 'queued',
  cancelled: 'archived',
};

/** The per-line status the backend reports. */
const LINE_STATUS: Record<string, StatusId> = {
  PENDING: 'queued',
  ASSIGNED: 'queued',
  RUNNING: 'running',
  REVIEWING: 'running',
  PASSED: 'completed',
  BLOCKED: 'blocked',
  FAILED: 'failed',
};

/**
 * The status a word names.
 *
 * A dispatch state, a gate phase and a line status are three vocabularies
 * that share seven outcomes, so they fold into one id and the page prints
 * one glyph and one word for all of them.
 */
export function statusIdOf(value: unknown): StatusId {
  const key = clip(value, 40).toLowerCase();
  if (!key) return 'queued';
  if (STATE_STATUS[key.toUpperCase()]) return STATE_STATUS[key.toUpperCase()];
  if (PHASE_STATUS[key]) return PHASE_STATUS[key];
  if (LINE_STATUS[key.toUpperCase()]) return LINE_STATUS[key.toUpperCase()];
  return 'queued';
}

export function statusMark(value: unknown): StatusMark {
  const id = statusIdOf(value);
  return { id, glyph: STATUS_GLYPHS[id], tone: STATUS_TONES[id] };
}

export type VerdictId = 'merge' | 'block' | 'pending' | 'approved';

export const VERDICT_GLYPHS: Record<VerdictId, string> = {
  merge: '✔',
  block: '✖',
  pending: '○',
  approved: '✔',
};

export const VERDICT_TONES: Record<VerdictId, Tone> = {
  merge: 'ok',
  block: 'error',
  pending: 'muted',
  approved: 'ok',
};

export interface VerdictMark {
  id: VerdictId;
  glyph: string;
  tone: Tone;
}

/**
 * What one review record says.
 *
 * The backend records the stage verdict of a review: `merge` when the
 * review was the last word, `approve` when a risk bearing primary review
 * passed but a secondary review is still to come, and `block` when it
 * rejected the branch tip. A gate cell only says pass or fail, so it is
 * read as `pass`.
 */
export function reviewOutcome(value: unknown): VerdictId {
  const key = clip(value, 40).toLowerCase();
  if (!key) return 'pending';
  if (key === 'merge') return 'merge';
  if (key === 'approve') return 'approved';
  if (key === 'block' || key === 'blocked' || key === 'fail' || key === 'failed' || key === 'reject') {
    return 'block';
  }
  if (key === 'pass' || key === 'passed') return 'merge';
  return 'pending';
}

/** A verdict word, or the pending mark when there is none yet. */
export function verdictIdOf(value: unknown): VerdictId {
  return reviewOutcome(value);
}

export function verdictMark(value: unknown): VerdictMark {
  const id = verdictIdOf(value);
  return { id, glyph: VERDICT_GLYPHS[id], tone: VERDICT_TONES[id] };
}

/** The gates whose records are reviews. */
const REVIEW_GATES = ['primary_review', 'secondary_review'] as const;

/** The last review record of one gate, from the history the backend keeps. */
function lastReview(record: GateRecord | null, gate: string): ReviewRecord | null {
  const history = record?.history ?? [];
  for (let index = history.length - 1; index >= 0; index -= 1) {
    const item = history[index];
    if (item?.gate === gate) return item;
  }
  return null;
}

/**
 * Whether a risk bearing line still waits for its secondary review.
 *
 * The backend asks for a secondary review when the line is risk bearing, and
 * the phase names that wait. Without that wait, one review is the last word.
 */
export function secondaryStillToCome(record: GateRecord | null): boolean {
  if (!record) return false;
  if (record.risk_bearing === false) return false;
  const phase = clip(record.phase || record.machine?.stage, 40).toLowerCase();
  return phase === 'secondary_review';
}

/**
 * The verdict of one line.
 *
 * The secondary review is the last word on a risk bearing line, so it wins.
 * A primary review that recorded `approve` says the primary passed and the
 * secondary review is still to come: the line's verdict is not final yet, so
 * it stays pending until that review speaks.
 */
export function lineVerdict(record: GateRecord | null): VerdictMark {
  if (!record) return verdictMark('pending');

  const secondary = lastReview(record, 'secondary_review');
  if (secondary) {
    const value = secondary.verdict ?? secondary.result;
    if (value) return verdictMark(value);
  }
  const secondaryCell = record.gates?.secondary_review;
  if (secondaryCell && (secondaryCell.verdict || secondaryCell.result)) {
    return verdictMark(secondaryCell.verdict ?? secondaryCell.result);
  }

  const primary = lastReview(record, 'primary_review');
  const primaryValue = primary?.verdict ?? primary?.result;
  if (primaryValue) {
    if (verdictIdOf(primaryValue) === 'approved' && secondaryStillToCome(record)) {
      return verdictMark('pending');
    }
    return verdictMark(primaryValue);
  }
  const primaryCell = record.gates?.primary_review;
  if (primaryCell && (primaryCell.verdict || primaryCell.result)) {
    const value = primaryCell.verdict ?? primaryCell.result;
    if (verdictIdOf(value) === 'merge' && secondaryStillToCome(record)) {
      return verdictMark('pending');
    }
    return verdictMark(value);
  }
  return verdictMark('pending');
}

/* ------------------------------------------------------------------ *
 * Provider and model
 * ------------------------------------------------------------------ */

export interface ProviderCatalog {
  /** Model key → provider id, as the backend's model catalog names it. */
  byModel: Record<string, string>;
  /** Every provider id the backend knows. */
  providers: string[];
}

export const EMPTY_PROVIDER_CATALOG: ProviderCatalog = { byModel: {}, providers: [] };

/**
 * The provider that serves a model.
 *
 * The catalog is the authority. A model the catalog does not name falls back
 * to its first segment only when that segment is a provider the catalog
 * knows, so a key like `gpt-5.6-terra` never becomes a provider called `gpt`.
 */
export function providerOf(
  model: string | undefined | null,
  catalog: ProviderCatalog = EMPTY_PROVIDER_CATALOG,
): string {
  const key = String(model ?? '').trim();
  if (!key) return '';
  const named = catalog.byModel[key];
  if (named) return named;
  const head = key.split(/[-_./]/)[0]?.toLowerCase() ?? '';
  if (head && catalog.providers.includes(head)) return head;
  return '';
}

export interface LineActors {
  worker: { provider: string; model: string };
  reviewer: { provider: string; model: string } | null;
}

/**
 * Who works a line and who reviews it.
 *
 * The worker is the model the planner assigned. The reviewer is the one the
 * gate recorded — never the worker itself, because a line never reviews
 * itself.
 */
export function lineActors(
  summary: DispatchSummary,
  line: DispatchLine,
  catalog: ProviderCatalog = EMPTY_PROVIDER_CATALOG,
): LineActors {
  const record = gateRecord(summary, lineId(line));
  const workerModel = String(line.assigned_model || record?.author || '').trim();
  const reviewerModel = String(
    record?.primary_reviewer
      || record?.gates?.primary_review?.reviewer
      || record?.machine?.primary_reviewer
      || '',
  ).trim();
  const worker = { provider: providerOf(workerModel, catalog), model: workerModel };
  const reviewer = reviewerModel
    ? { provider: providerOf(reviewerModel, catalog), model: reviewerModel }
    : null;
  return { worker, reviewer };
}

/* ------------------------------------------------------------------ *
 * The human gate
 * ------------------------------------------------------------------ */

/** The phase of one line, as the gate recorded it. */
export function linePhase(summary: DispatchSummary, line: DispatchLine): string {
  const record = gateRecord(summary, lineId(line));
  const phase = String(record?.phase || record?.machine?.stage || '').trim();
  if (phase) return phase;
  return String(line.status || '').trim();
}

/**
 * Whether the dispatch waits for a person.
 *
 * The backend parks a dispatch at `APPROVAL`; the gate phase of the line is
 * `awaiting_human`. Either one means the same thing, so both are read. A
 * dispatch that already reached a terminal state never waits again, whatever
 * its last line recorded.
 */
export function isAwaitingHuman(summary: DispatchSummary | null | undefined): boolean {
  if (!summary) return false;
  const state = String(summary.state || '').toUpperCase();
  if (TERMINAL_STATES.has(state)) return false;
  if (state === 'APPROVAL') return true;
  return (summary.lines ?? []).some((line) => linePhase(summary, line) === 'awaiting_human');
}

/**
 * The reassign queued for one line, when there is one.
 *
 * While a drive owns the dispatch the backend queues a reassign instead of
 * applying it; the drive applies it at its next step. The model named here
 * is not the line's model yet.
 */
export function queuedReassign(summary: DispatchSummary | null | undefined, id: string): string | null {
  const queued = summary?.human_reassigns?.queued;
  if (!Array.isArray(queued) || !id) return null;
  const entry = queued.find(
    (item) => item && item.line_id === id && (item.kind ?? 'reassign') === 'reassign',
  );
  if (!entry) return null;
  return String(entry.model ?? '').trim() || null;
}

/** The line the decision is about: the one waiting for a person, else the first. */
export function focusLine(summary: DispatchSummary): DispatchLine | null {
  const lines = summary.lines ?? [];
  const waiting = lines.find((line) => linePhase(summary, line) === 'awaiting_human');
  return waiting ?? lines[0] ?? null;
}

/** The gate record of the line the decision is about. */
export function focusRecord(summary: DispatchSummary): GateRecord | null {
  const line = focusLine(summary);
  return line ? gateRecord(summary, lineId(line)) : null;
}

/* ------------------------------------------------------------------ *
 * The dispatch history
 *
 * The dispatches this user has seen or started, kept in the browser and
 * read back one dispatch at a time. There is no route that lists them, so
 * the ids are the record and every row is loaded through the one route
 * that reads a dispatch.
 * ------------------------------------------------------------------ */

const HISTORY_KEY = 'dispatch.history';
const HISTORY_LIMIT = 12;

function readStored(): string[] {
  const raw = userLocalStorage.getItem(HISTORY_KEY);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.map((item) => String(item ?? '').trim()).filter(Boolean);
  } catch {
    return [];
  }
}

/** The dispatch ids this user has seen or started, newest first. */
export function readDispatchHistory(): string[] {
  const seen: string[] = [];
  for (const id of readStored()) {
    if (!seen.includes(id)) seen.push(id);
  }
  return seen.slice(0, HISTORY_LIMIT);
}

/** Remember a dispatch: the newest first, the oldest dropped past the limit. */
export function rememberDispatch(id: string): string[] {
  const key = String(id ?? '').trim();
  if (!key) return readDispatchHistory();
  const next = [key, ...readDispatchHistory().filter((item) => item !== key)].slice(0, HISTORY_LIMIT);
  userLocalStorage.setItem(HISTORY_KEY, JSON.stringify(next));
  return next;
}

/** Drop a dispatch the backend no longer serves. */
export function forgetDispatch(id: string): string[] {
  const next = readDispatchHistory().filter((item) => item !== id);
  userLocalStorage.setItem(HISTORY_KEY, JSON.stringify(next));
  return next;
}

/* ------------------------------------------------------------------ *
 * The review verdicts table
 * ------------------------------------------------------------------ */

export interface VerdictRow {
  lineId: string;
  gate: string;
  reviewer: string;
  verdict: VerdictMark;
  blockerCount: number;
  round: number;
}

/** The gate names that carry a review verdict, in the order the design lists them. */
const VERDICT_GATES = REVIEW_GATES;

/**
 * One row per review record of the dispatch.
 *
 * The backend keeps every review round in the line's history, with the round
 * it was and how many findings it carried; the gate cell only holds the
 * current state of that gate. So the history is what the table shows, and a
 * line that was blocked twice carries both rounds: the verdict that blocked
 * it and the verdict that let it through.
 */
export function verdictRows(summary: DispatchSummary): VerdictRow[] {
  const rows: VerdictRow[] = [];
  for (const line of summary.lines ?? []) {
    const id = lineId(line);
    const record = gateRecord(summary, id);
    if (!record) continue;
    for (const gate of VERDICT_GATES) {
      const rounds = (record.history ?? []).filter((item) => item?.gate === gate);
      for (const item of rounds) {
        rows.push({
          lineId: id,
          gate,
          reviewer: String(item.reviewer ?? reviewerOf(record, gate) ?? ''),
          verdict: verdictMark(item.verdict ?? item.result),
          blockerCount: Number(item.blocker_count ?? 0),
          round: Number(item.round ?? 0),
        });
      }
      if (rounds.length) continue;
      // No history for that gate: the cell is the only record of it.
      const check = record.gates?.[gate];
      if (!check || (!check.verdict && !check.result)) continue;
      rows.push({
        lineId: id,
        gate,
        reviewer: String(check.reviewer ?? reviewerOf(record, gate) ?? ''),
        verdict: verdictMark(check.verdict ?? check.result),
        blockerCount: Number(check.blocker_count ?? 0),
        round: Number(check.round ?? 0),
      });
    }
  }
  return rows;
}

/** The reviewer the line's gate record names for one gate. */
function reviewerOf(record: GateRecord, gate: string): string | undefined {
  if (gate === 'secondary_review') return record.secondary_reviewer;
  return record.primary_reviewer;
}

/**
 * The revision the last review judged, when the backend recorded one.
 *
 * The head a rejecting review saw is the commit its verdict is stamped on.
 * When no review has rejected the tip, the revision the focused tests pinned
 * is the revision the line was judged at. A line with neither has no known
 * reviewed revision, and the page says so rather than naming another commit.
 */
export function reviewedCommit(record: GateRecord | null): string {
  const head = record?.last_review_blockers?.head;
  if (typeof head === 'string' && head.trim()) return head.trim();
  const revision = record?.gates?.G2?.revision;
  if (typeof revision === 'string' && revision.trim()) return revision.trim();
  return '';
}

/* ------------------------------------------------------------------ *
 * The named axis
 * ------------------------------------------------------------------ */

/** Named positions. G1, G3 and G4 are not stages and are not drawn. */
const TIMELINE_AXIS = [
  { id: 'g0', phases: ['planning', 'spec_review'], gates: ['g0'] },
  { id: 'd1', phases: ['dag_ready'], gates: ['d1'] },
  { id: 'implementing', phases: ['assigned', 'implementing'], gates: [] },
  { id: 'g2', phases: ['focused_tests', 'g2'], gates: ['g2'] },
  { id: 'primary', phases: ['primary_review'], gates: ['primary_review'] },
  { id: 'secondary', phases: ['secondary_review'], gates: ['secondary_review'] },
  { id: 'g5', phases: ['integration_checks', 'g5'], gates: ['g5'] },
  { id: 'g6', phases: ['awaiting_human', 'approval', 'g6'], gates: ['g6'] },
] as const;

const PRIMARY_REASONS = new Set(['review_blocked', 'fix_resubmitted', 'primary_review_passed']);
const LINE_MARKERS = new Set(['blocked', 'replanning', 'cancelled']);

export interface TimelineSlot {
  id: string;
  status: string;
  detail: string;
  notes: string[];
  current: boolean;
}

function token(value: unknown): string {
  return clip(value, 80).toLowerCase();
}

function slotIdFor(value: unknown): string | null {
  const key = token(value);
  if (!key) return null;
  for (const slot of TIMELINE_AXIS) {
    if ((slot.phases as readonly string[]).includes(key) || (slot.gates as readonly string[]).includes(key)) {
      return slot.id;
    }
  }
  return null;
}

function eventSlot(event: NonNullable<GateRecord['events']>[number]): string | null {
  if (token(event.gate)) return slotIdFor(event.gate);
  const reason = token(event.reason);
  const stages = [token(event.from_stage), token(event.to_stage)];
  if (reason.startsWith('secondary_') || (stages.includes('secondary_review') && (reason === 'review_blocked' || reason === 'fix_resubmitted'))) {
    return 'secondary';
  }
  if (PRIMARY_REASONS.has(reason)) return 'primary';
  for (const stage of [event.to_stage, event.from_stage]) {
    const found = slotIdFor(stage);
    if (found) return found;
  }
  return null;
}

function historySlot(item: NonNullable<GateRecord['history']>[number]): string | null {
  const found = slotIdFor(item.gate);
  if (found) return found;
  const reason = token(item.reason);
  if (reason.startsWith('secondary_')) return 'secondary';
  if (PRIMARY_REASONS.has(reason)) return 'primary';
  return null;
}

function checkFor(record: GateRecord, slotId: string): GateCheck | undefined {
  for (const [name, item] of Object.entries(record.gates ?? {})) {
    if (item && typeof item === 'object' && slotIdFor(name) === slotId) return item;
  }
  return undefined;
}

function eventStop(event: NonNullable<GateRecord['events']>[number]): string | null {
  const from = slotIdFor(event.from_stage);
  if (from) return from;
  const gate = token(event.gate);
  if (gate === 'g0' || gate === 'd1' || gate === 'g2' || gate === 'g5' || gate === 'g6') return gate;
  if (!gate) {
    const origin = token(event.from_stage);
    if (origin === 'primary_review') return 'primary';
    if (origin === 'secondary_review') return 'secondary';
  }
  return null;
}

function stopPosition(record: GateRecord, stage: string): string {
  const events = record.events ?? [];
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (token(events[index].to_stage) !== stage) continue;
    const found = eventStop(events[index]);
    if (found) return found;
  }
  const history = record.history ?? [];
  for (let index = history.length - 1; index >= 0; index -= 1) {
    if (!token(history[index].result)) continue;
    const found = historySlot(history[index]);
    if (found) return found;
  }
  return 'implementing';
}

export function timelineOf(record: GateRecord | null): TimelineSlot[] {
  const machineStage = token(record?.machine?.stage);
  const stage = machineStage || token(record?.phase);
  const back = token(record?.return_stage || record?.machine?.return_stage);
  let marker: string | null = null;
  let currentId: string | null = null;
  if (stage === 'merged') marker = 'merged';
  else if (stage === 'fixing') {
    marker = 'fixing';
    currentId = slotIdFor(back) || 'primary';
  } else if (LINE_MARKERS.has(stage)) {
    marker = stage;
    currentId = record ? stopPosition(record, stage) : 'implementing';
  } else currentId = slotIdFor(stage);

  const notes = new Map<string, string[]>(TIMELINE_AXIS.map((slot) => [slot.id, []]));
  const seen = new Map<string, Set<string>>(TIMELINE_AXIS.map((slot) => [slot.id, new Set()]));
  const eventResult = new Map<string, string>();
  const historyResult = new Map<string, string>();
  const remember = (slotId: string | null, value: unknown) => {
    if (!slotId) return;
    const text = clip(value, 120);
    const bag = seen.get(slotId);
    if (!text || !bag || bag.has(text)) return;
    bag.add(text);
    notes.get(slotId)?.push(text);
  };
  for (const item of record?.history ?? []) {
    const slotId = historySlot(item);
    remember(slotId, item.reason);
    const result = clip(item.result, 40);
    if (slotId && result) historyResult.set(slotId, result);
  }
  for (const event of record?.events ?? []) {
    const slotId = eventSlot(event);
    remember(slotId, event.reason);
    const result = clip(event.result, 40);
    if (slotId && result) eventResult.set(slotId, result);
  }

  const currentIndex = TIMELINE_AXIS.findIndex((slot) => slot.id === currentId);
  return TIMELINE_AXIS.map((slot, index) => {
    const check = record ? checkFor(record, slot.id) : undefined;
    const result = check?.result
      ? clip(check.result, 40)
      : eventResult.get(slot.id) || historyResult.get(slot.id) || '';
    let detail = check?.reason ? clip(check.reason, 120) : '';
    let trail = (notes.get(slot.id) ?? []).filter((item) => item !== detail);
    let here = slot.id === currentId;
    let status = 'unreached';
    if (marker === 'merged') status = 'merged';
    else if (marker && LINE_MARKERS.has(marker) && currentIndex >= 0 && index > currentIndex) {
      status = 'unreached';
      detail = '';
      trail = [];
      here = false;
    } else if (here && marker === 'fixing') status = 'fixing';
    else if (here && marker && LINE_MARKERS.has(marker)) status = marker;
    else if (
      here && slot.id === 'g6'
      && (stage === 'awaiting_human' || stage === 'approval' || stage === 'g6')
      && (!result || result.toLowerCase() === 'pending')
      && (!detail || detail.toLowerCase() === 'awaiting_human' || stage === 'awaiting_human' || stage === 'approval')
    ) status = 'pending-approval';
    else if (result) status = result;
    else if (here) status = 'current';
    else if (slot.id === 'secondary' && record?.risk_bearing === false && !detail && trail.length === 0) {
      status = 'not-applicable';
    } else if (detail || trail.length) status = '—';
    return { id: slot.id, status, detail, notes: trail, current: here };
  });
}
