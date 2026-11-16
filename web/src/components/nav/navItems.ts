import { Activity, Bot, GitBranch, Inbox, ListTodo, Radio, Server, Send, Settings, Blocks, Wrench } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
// Register tnShell before any labelKey below is resolved.
import '../AppShell/shellLocales';

export interface NavItem {
  /** Route path. */
  key: string;
  icon: LucideIcon;
  /** i18n key resolved via t(). */
  labelKey: string;
  /** Active-state strategy: 'prefix' = pathname.startsWith(key); 'exact-or-sub' = exact match or key + '/' sub-path. */
  match: 'prefix' | 'exact-or-sub';
  aliases?: string[];
}

// Every destination gets its own glyph: the rail must be scannable by icon,
// and /lines, /dispatch and /agent-fleet used to share GitBranch (same for
// /plugins and /skills-mcp). The unit test beside this file pins the rule.
const BASE_NAV_ITEMS: NavItem[] = [
  { key: '/inbox', icon: Inbox, labelKey: 'tnShell.nav.inbox', match: 'exact-or-sub' },
  { key: '/tasks', icon: ListTodo, labelKey: 'tnShell.nav.tasks', match: 'exact-or-sub' },
  { key: '/agents', icon: Bot, labelKey: 'tnShell.nav.agents', match: 'exact-or-sub' },
  { key: '/lines', icon: GitBranch, labelKey: 'tnShell.nav.lines', match: 'exact-or-sub' },
  { key: '/dispatch', icon: Send, labelKey: 'tnShell.nav.dispatch', match: 'exact-or-sub' },
  { key: '/agent-fleet', icon: Server, labelKey: 'tnShell.nav.agentFleet', match: 'exact-or-sub' },
  { key: '/runs', icon: Activity, labelKey: 'tnShell.nav.runs', match: 'exact-or-sub' },
  { key: '/sources', icon: Radio, labelKey: 'tnShell.nav.sources', match: 'exact-or-sub' },
  { key: '/plugins', icon: Blocks, labelKey: 'tnShell.nav.plugins', match: 'exact-or-sub' },
  { key: '/skills-mcp', icon: Wrench, labelKey: 'tnShell.nav.skillsMcp', match: 'exact-or-sub' },
];

export const NAV_ITEMS: NavItem[] = BASE_NAV_ITEMS;

export const SETTINGS_ITEM: NavItem = {
  key: '/settings',
  icon: Settings,
  labelKey: 'tnShell.nav.settings',
  match: 'exact-or-sub',
};
