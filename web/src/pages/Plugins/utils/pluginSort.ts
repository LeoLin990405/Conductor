import type { PluginInfo } from '@/api/core';
import type { ListSortKey } from './listParams';

/**
 * How the Plugins list orders itself. `recommended` keeps the order the tab
 * hands in (problems first is the sections' own reading, name below it);
 * `name` sorts what the tab has as it stands; `recent` puts what arrived (or
 * last changed) most recently first.
 *
 * A package the server dates neither way sorts last rather than first: an
 * absent date is not "just now".
 */

function arrivedAt(plugin: PluginInfo): number | null {
  const raw = plugin.installed_at ?? plugin.updated_at;
  if (!raw) return null;
  const at = Date.parse(raw);
  return Number.isNaN(at) ? null : at;
}

export function pluginOrder(a: PluginInfo, b: PluginInfo, key: ListSortKey): number {
  if (key === 'name') return a.name.localeCompare(b.name);
  if (key === 'recent') {
    const at = arrivedAt(a);
    const bt = arrivedAt(b);
    if (at === null && bt === null) return a.name.localeCompare(b.name);
    // Undated packages keep a stable place at the end.
    if (at === null) return 1;
    if (bt === null) return -1;
    return bt - at || a.name.localeCompare(b.name);
  }
  return 0;
}
