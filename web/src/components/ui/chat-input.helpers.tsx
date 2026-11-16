import {
  Blocks,
  Bot,
  BrainCircuit,
  Compass,
  Database,
  Eraser,
  HardDriveDownload,
  LayoutDashboard,
  Plus,
  Settings,
  Shrink,
  Terminal,
  Timer,
} from 'lucide-react';
import type { SlashCommand } from './chat-input.types';
import type { ModelMetadataEntry } from '@/hooks/useFilteredModels';

/** Return the appropriate icon for a slash command. */
export function getSlashCommandIcon(cmd: SlashCommand, className: string) {
  switch (cmd.name) {
    case 'dashboard': return <LayoutDashboard className={className} />;
    case 'memory': return <BrainCircuit className={className} />;
    case 'automations': return <Timer className={className} />;
    case 'plugins': return <Blocks className={className} />;
    case 'data': return <Database className={className} />;
    case 'settings': return <Settings className={className} />;
    case 'new': return <Plus className={className} />;
    case 'clear': return <Eraser className={className} />;
    case 'offload': return <HardDriveDownload className={className} />;
    case 'compact': return <Shrink className={className} />;
  }
  if (cmd.type === 'subagent') return <Bot className={className} />;
  if (cmd.type === 'action') return <Shrink className={className} />;
  if (cmd.type === 'navigation') return <Compass className={className} />;
  return <Terminal className={className} />;
}

/* --- CONSTANTS --- */
export const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB
export const MAX_FILES = 5;
export const PASTE_PILL_MIN_CHARS = 1500;
export const PASTE_PILL_MIN_LINES = 15;

/**
 * A paste this large would flood the draft — the composer stages it as a
 * removable pill instead and flattens it back into the message on send.
 */
export function isLargePaste(text: string): boolean {
  if (text.length >= PASTE_PILL_MIN_CHARS) return true;
  let lines = 1;
  for (let i = 0; i < text.length; i++) {
    if (text[i] === '\n' && ++lines >= PASTE_PILL_MIN_LINES) return true;
  }
  return false;
}
export const SLASH_GROUP_ORDER = ['system', 'agent', 'navigate', 'skill'] as const;

/**
 * Commands that ship with the composer. Skills append themselves under the
 * `skill` group at runtime. Navigation commands carry a route and execute
 * immediately; action commands stage a pill and run on send (except /clear,
 * handled by the composer itself).
 */
export const BUILTIN_SLASH_COMMANDS: SlashCommand[] = [
  { type: 'action', name: 'compact', group: 'system', aliases: ['compaction', 'summarize'] },
  { type: 'action', name: 'offload', group: 'system', aliases: ['truncate'] },
  { type: 'action', name: 'clear', group: 'system', aliases: ['reset'] },
  { type: 'subagent', name: 'subagent', group: 'agent' },
  { type: 'navigation', name: 'new', group: 'navigate', route: '/chat' },
  { type: 'navigation', name: 'dashboard', group: 'navigate', route: '/dashboard' },
  { type: 'navigation', name: 'memory', group: 'navigate', route: '/memory' },
  { type: 'navigation', name: 'automations', group: 'navigate', route: '/automations' },
  { type: 'navigation', name: 'plugins', group: 'navigate', route: '/plugins' },
  { type: 'navigation', name: 'data', group: 'navigate', route: '/settings?tab=data' },
  { type: 'navigation', name: 'settings', group: 'navigate', route: '/settings' },
  { type: 'navigation', name: 'model', group: 'navigate', route: '/settings?tab=model' },
];

/** Group headers in menu order, keeping each command's registry order. */
export function groupSlashCommands(
  commands: SlashCommand[],
): Array<{ group: string; commands: SlashCommand[] }> {
  const byGroup = new Map<string, SlashCommand[]>();
  for (const command of commands) {
    const group = command.group ?? 'system';
    const rows = byGroup.get(group) ?? [];
    rows.push(command);
    byGroup.set(group, rows);
  }
  // First-seen order, not the fixed group order: the caller already sorted by
  // relevance, and re-sorting by group would put a system command that only
  // substring-matches ahead of a prefix skill. Empty-query menus still come out
  // in registry order because every item ties on rank and the sort is stable.
  return [...byGroup.entries()].map(([group, rows]) => ({
    group,
    commands: rows,
  })) as Array<{ group: string; commands: SlashCommand[] }>;
}

export interface TypedSlashCommand {
  command: SlashCommand;
  /** Inclusive start of the `/name` token. */
  start: number;
  /** Exclusive end of the token. */
  end: number;
}

/**
 * Commands typed into the draft, including aliases. Matching only consumes a
 * known command name at a word boundary, so a path like `/Users/me` or a URL
 * cannot accidentally execute something.
 */
export function extractTypedSlashCommands(
  value: string,
  commands: SlashCommand[],
): TypedSlashCommand[] {
  const byName = new Map<string, SlashCommand>();
  for (const command of commands) {
    byName.set(command.name.toLowerCase(), command);
    for (const alias of command.aliases ?? []) byName.set(alias.toLowerCase(), command);
  }
  const found: TypedSlashCommand[] = [];
  const seen = new Set<string>();
  const re = /(^|\s)(\/[a-z0-9][a-z0-9-]*)/gi;
  let match: RegExpExecArray | null;
  while ((match = re.exec(value)) !== null) {
    const token = match[2].slice(1).toLowerCase();
    const command = byName.get(token);
    if (!command || seen.has(command.name)) continue;
    seen.add(command.name);
    const start = match.index + match[1].length;
    found.push({ command, start, end: start + match[2].length });
  }
  return found;
}

/** Remove the typed command tokens from a draft, preserving the rest. */
export function stripTypedSlashCommands(value: string, typed: TypedSlashCommand[]): string {
  let out = value;
  for (const { start, end } of [...typed].sort((a, b) => b.start - a.start)) {
    out = out.slice(0, start) + out.slice(end);
  }
  return out.replace(/[ \t]{2,}/g, ' ').trim();
}

/**
 * Index of the `@`/`/` token the cursor is currently inside, or -1.
 *
 * Scans left from the cursor and stops at the first whitespace: the trigger
 * only counts at a word start (beginning of the draft or after whitespace), so
 * `foo@bar` and a URL's slashes never open a menu.
 */
export function findTriggerIndex(value: string, cursorPos: number, trigger: '@' | '/'): number {
  for (let i = cursorPos - 1; i >= 0; i--) {
    const ch = value[i];
    if (ch === trigger) {
      return i === 0 || /\s/.test(value[i - 1]) ? i : -1;
    }
    if (/\s/.test(ch)) return -1;
  }
  return -1;
}

/**
 * Slash-menu sort key (lower sorts first). Prefix matches (name/alias starts
 * with the query, char-by-char) rank above substring-only matches; within a
 * tier, system/agent commands rank above skills, and skills above navigation
 * (a search for /d should find a doc-writer skill before dashboard, but /dashboard
 * still follows).
 */
const SLASH_GROUP_RANK: Record<string, number> = {
  system: 0,
  agent: 1,
  skill: 2,
  navigate: 3,
};

export function slashRank(item: SlashCommand, query: string): number {
  const isPrefix =
    item.name.toLowerCase().startsWith(query) ||
    !!item.aliases?.some((a) => a.toLowerCase().startsWith(query));
  return (isPrefix ? 0 : 10) + (SLASH_GROUP_RANK[item.group ?? 'system'] ?? 4);
}

/** Short display name for a model key: the manifest's authored name when the
 *  metadata carries one, else one derived from the key. */
export function getModelDisplayName(key: string | null, metadata?: Record<string, ModelMetadataEntry>): string {
  if (!key) return '';
  const authored = metadata?.[key]?.display_name;
  if (authored) return authored;
  let name = key;
  // Strip common provider prefixes
  for (const prefix of ['claude-', 'gpt-', 'chatgpt-', 'o1-', 'o3-', 'o4-']) {
    if (name.startsWith(prefix)) { name = name.slice(prefix.length); break; }
  }
  // Convert version-like patterns: "opus-4-6" → "Opus 4.6", "sonnet-4-6" → "Sonnet 4.6"
  name = name
    .replace(/-(\d+)-(\d+)/, ' $1.$2')
    .replace(/-(\d+\.\d+)/, ' $1')
    .replace(/-/g, ' ')
    .replace(/\b\w/g, (c: string) => c.toUpperCase());
  return name;
}
