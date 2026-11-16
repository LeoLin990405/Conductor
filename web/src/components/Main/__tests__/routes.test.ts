// @vitest-environment node
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { NAV_ITEMS, SETTINGS_ITEM } from '../../nav/navItems';

/** S12 owns Main.tsx but must not add, remove or rename a route: pages are
 *  restyled in place. The list below is the route table as S12 found it.
 *  `/memory` moved up beside the live pages when it stopped being a retired
 *  surface and became the browse-by-concept page; the route set is the same. */
const ROUTES = [
  '/',
  '/inbox',
  '/tasks',
  '/tasks/:ticketId',
  '/agents',
  '/lines',
  '/dispatch',
  '/dispatch/:dispatchId',
  '/agent-fleet',
  '/runs',
  '/sources',
  '/plugins',
  '/skills-mcp',
  '/connectors',
  '/settings',
  '/memory',
  '/superagent',
  '/chat/*',
  '/projects/*',
  '/automations',
  '*',
];

function routePaths(): string[] {
  const src = readFileSync(new URL('../Main.tsx', import.meta.url), 'utf8');
  return [...src.matchAll(/<Route\s+path="([^"]+)"/g)].map((m) => m[1]);
}

describe('Main.tsx route table', () => {
  it('keeps every route path unchanged', () => {
    expect(routePaths()).toEqual(ROUTES);
  });

  it('routes every nav destination', () => {
    const paths = new Set(routePaths());
    for (const item of [...NAV_ITEMS, SETTINGS_ITEM]) expect(paths.has(item.key)).toBe(true);
  });
});
