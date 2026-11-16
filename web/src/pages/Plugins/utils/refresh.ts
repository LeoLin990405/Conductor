import type { QueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import type { Section } from '../components/shell/sections';
import type { SectionView } from './detailParam';

/**
 * What a refresh on the Plugins page re-reads, per section.
 *
 * Every section renders from more than its own list: a plugin's problem is a
 * verdict over the MCP catalog, a connector's menu may open a plugin's page,
 * and Discover's installed marks come from the plugin list itself. Refreshing
 * only the list the section mounts would leave those beside it saying what
 * they said before, so each section names the reads it draws from.
 *
 * A prefix key invalidates its whole family (`plugins.all` covers the list and
 * the marketplace), which is exactly the radius a manual refresh means: the
 * user asking again for what is on screen, not a surgical invalidation.
 */
const SECTION_KEYS: Record<Section, readonly (readonly unknown[])[]> = {
  plugins: [queryKeys.plugins.all, queryKeys.mcp.all],
  mcp: [queryKeys.mcp.all, queryKeys.plugins.all],
  skills: [queryKeys.skills.all, queryKeys.plugins.all],
  subagents: [queryKeys.subagents.all, queryKeys.plugins.all],
  secrets: [queryKeys.userVault.all],
};

/** Discover's own reads: the catalog listing and what its cards install. */
const DISCOVER_KEYS: readonly (readonly unknown[])[] = [
  queryKeys.plugins.all,
  queryKeys.mcp.all,
];

export function refreshKeys(
  section: Section,
  view: SectionView,
): readonly (readonly unknown[])[] {
  return view === 'discover' ? DISCOVER_KEYS : SECTION_KEYS[section];
}

/** Re-read everything the section on screen draws from, once. */
export async function refreshSection(
  queryClient: Pick<QueryClient, 'invalidateQueries'>,
  section: Section,
  view: SectionView,
): Promise<void> {
  await Promise.all(
    refreshKeys(section, view).map((queryKey) => queryClient.invalidateQueries({ queryKey })),
  );
}
