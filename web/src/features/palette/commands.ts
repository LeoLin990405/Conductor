/**
 * Palette command table — kept in lockstep with tokens.json palette.commands
 * / idleOrder via commandsSync.test.ts. Copy registers through featureLocales.
 */
import { registerFeatureLocales } from '@/lib/featureLocales';
import en from './locales/tnPalette.en-US.json';
import zh from './locales/tnPalette.zh-CN.json';

registerFeatureLocales({ 'en-US': en, 'zh-CN': zh });

export type PaletteCommand = {
  name: string;
  slash: string | null;
  aliases: string[];
  hint: string;
  /** Bare form cannot run; Tab/Enter insert a trailing space. */
  requiresArg: boolean;
};

export const COMPLETION_LIMIT = 60;

export const COMMANDS: readonly PaletteCommand[] = [
  {
    name: 'workers',
    slash: '/worker',
    aliases: ['worker'],
    hint: 'show which worker has the current Ticket',
    requiresArg: false,
  },
  {
    name: 'dispatch',
    slash: '/dispatch',
    aliases: [],
    hint: 'dispatch roster; /dispatch <id> reads one',
    requiresArg: false,
  },
  {
    name: 'approve',
    slash: null,
    aliases: [],
    hint: 'approve a dispatch that is waiting for a person',
    requiresArg: true,
  },
  {
    name: 'reject',
    slash: null,
    aliases: [],
    hint: 'reject a dispatch that is waiting for a person',
    requiresArg: true,
  },
  {
    name: 'tickets',
    slash: '/jobs',
    aliases: ['/j', 'jobs', '/attach'],
    hint: 'list Tickets; /jobs <id> shows one; /jobs watch <id> follows it',
    requiresArg: false,
  },
  {
    name: 'retry',
    slash: '/retry',
    aliases: [],
    hint: 'retry a failed or waiting Ticket',
    requiresArg: false,
  },
  {
    name: 'review',
    slash: '/review',
    aliases: [],
    hint: 'queue a local cross-review (/review run <line> --by <line>)',
    requiresArg: true,
  },
  {
    name: 'memory',
    slash: '/memory',
    aliases: [],
    hint: 'browse remembered memory',
    requiresArg: false,
  },
  {
    name: 'workspace',
    slash: '/workspace',
    aliases: [],
    hint: 'switch the Home workspace',
    requiresArg: false,
  },
  {
    name: 'reply',
    slash: '/reply',
    aliases: [],
    hint: 'answer the Ticket that needs you',
    requiresArg: true,
  },
  {
    name: 'cancel',
    slash: '/cancel',
    aliases: [],
    hint: 'stop a running Ticket (/cancel <id|index>)',
    requiresArg: false,
  },
  {
    name: 'resume',
    slash: '/resume',
    aliases: [],
    hint: 'list saved sessions; /resume <id> reopens one',
    requiresArg: false,
  },
  {
    name: 'status',
    slash: '/status',
    aliases: [],
    hint: 'reread Supervisor progress for the current Ticket',
    requiresArg: false,
  },
  {
    name: 'help',
    slash: '/help',
    aliases: ['?'],
    hint: 'show the TUI key map and commands',
    requiresArg: false,
  },
  {
    name: 'quit',
    slash: '/quit',
    aliases: ['/exit', '/q', 'exit'],
    hint: 'quit the TUI',
    requiresArg: false,
  },
  {
    name: 'skills',
    slash: '/skills',
    aliases: [],
    hint: 'local skills status',
    requiresArg: false,
  },
  {
    name: 'mcp',
    slash: '/mcp',
    aliases: [],
    hint: 'local MCP server status',
    requiresArg: false,
  },
  {
    name: 'agents',
    slash: '/agents',
    aliases: [],
    hint: 'local fleet, native runtimes, daemons',
    requiresArg: false,
  },
  {
    name: 'merge',
    slash: '/merge',
    aliases: [],
    hint: 'merge a line locally after its tests (/merge <line> --into <branch>)',
    requiresArg: true,
  },
  {
    name: 'stop',
    slash: '/stop',
    aliases: [],
    hint: 'stop a running Ticket (/stop <id|index>)',
    requiresArg: true,
  },
  {
    name: 'signals',
    slash: '/signals',
    aliases: [],
    hint: 'Sources and job counts',
    requiresArg: false,
  },
  {
    name: 'clear',
    slash: '/clear',
    aliases: [],
    hint: 'clear the conversation',
    requiresArg: false,
  },
  {
    name: 'details',
    slash: '/details',
    aliases: [],
    hint: 'toggle Worker details (default: Supervisor view)',
    requiresArg: false,
  },
  {
    name: 'model',
    slash: '/model',
    aliases: [],
    hint: 'show the Router model and configured providers',
    requiresArg: false,
  },
  {
    name: 'compact',
    slash: '/compact',
    aliases: [],
    hint: 'drop activity noise and duplicate rows from the transcript',
    requiresArg: false,
  },
  {
    name: 'expand',
    slash: '/expand',
    aliases: [],
    hint: 'expand every tool block',
    requiresArg: false,
  },
  {
    name: 'collapse',
    slash: '/collapse',
    aliases: [],
    hint: 'collapse every tool block',
    requiresArg: false,
  },
  {
    name: 'new',
    slash: '/new',
    aliases: [],
    hint: 'start a fresh session (persisted)',
    requiresArg: false,
  },
] as const;

export const IDLE_ORDER: readonly string[] = [
  'workers',
  'dispatch',
  'approve',
  'reject',
  'tickets',
  'workspace',
  'reply',
  'retry',
  'cancel',
  'resume',
  'status',
  'help',
  'quit',
  'skills',
  'mcp',
  'memory',
  'agents',
  'review',
  'merge',
  'stop',
  'signals',
  'clear',
  'details',
  'model',
  'compact',
  'expand',
  'collapse',
  'new',
] as const;

const BY_NAME = new Map(COMMANDS.map((c) => [c.name, c]));

export function commandByName(name: string): PaletteCommand | undefined {
  return BY_NAME.get(name);
}

/** Resolve a typed token to its canonical command name. */
export function canonicalCommand(token: string): string | null {
  const t = token.replace(/^\//, '').toLowerCase();
  if (BY_NAME.has(t)) return t;
  for (const cmd of COMMANDS) {
    if (cmd.slash && cmd.slash.slice(1).toLowerCase() === t) return cmd.name;
    for (const alias of cmd.aliases) {
      if (alias.replace(/^\//, '').toLowerCase() === t) return cmd.name;
    }
  }
  return null;
}

export function idleCommands(): PaletteCommand[] {
  return IDLE_ORDER.map((name) => BY_NAME.get(name)!).filter(Boolean);
}
