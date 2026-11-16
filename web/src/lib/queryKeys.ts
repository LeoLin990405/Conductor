import type { QueryMeta } from '@tanstack/react-query';

/**
 * Hierarchical query key factory for React Query, plus the query `meta`
 * contract that rides alongside it (see {@link CACHE_ONLY_META}).
 *
 * Each level builds on its parent to enable prefix-based invalidation:
 *   invalidateQueries({ queryKey: queryKeys.user.all })
 *     → invalidates me, preferences, apiKeys
 *   invalidateQueries({ queryKey: queryKeys.workspaces.lists() })
 *     → invalidates all workspace list queries (any page/sort)
 */
export const queryKeys = {
  user: {
    all:         ['user'],
    me:          () => [...queryKeys.user.all, 'me'],
    preferences: () => [...queryKeys.user.all, 'preferences'],
    apiKeys:     () => [...queryKeys.user.all, 'api-keys'],
  },
  models: {
    all: ['models'],
  },
  dataSources: {
    all: ['data-sources'],
    codexbar: () => [...queryKeys.dataSources.all, 'codexbar'],
    connections: () => [...queryKeys.dataSources.all, 'connections'],
  },
  features: {
    all:  ['features'],
    list: () => [...queryKeys.features.all, 'list'],
  },
  autonomy: {
    all: ['autonomy'],
    overview: () => [...queryKeys.autonomy.all, 'overview'],
    daemons: () => [...queryKeys.autonomy.all, 'daemons'],
    sources: () => [...queryKeys.autonomy.all, 'sources'],
    tickets: () => [...queryKeys.autonomy.all, 'tickets'],
    lines: () => [...queryKeys.autonomy.all, 'lines'],
    ticket: (id: string) => [...queryKeys.autonomy.tickets(), id],
  },
  nativeAgents: {
    all: ['nativeAgents'],
    list: () => [...queryKeys.nativeAgents.all, 'list'],
  },
  mainAgent: {
    all: ['mainAgent'],
    statusBoard: () => [...queryKeys.mainAgent.all, 'statusBoard'],
  },
  agentFleet: {
    all: ['agentFleet'],
    fleet: () => [...queryKeys.agentFleet.all, 'fleet'],
    logs: (agentId: string) => [...queryKeys.agentFleet.all, 'logs', agentId],
  },
  platform: {
    all:    ['platform'],
    models: () => [...queryKeys.platform.all, 'models'],
  },
  oauth: {
    all:    ['oauth'],
    codex:  () => [...queryKeys.oauth.all, 'codex'],
    claude: () => [...queryKeys.oauth.all, 'claude'],
    grok:   () => [...queryKeys.oauth.all, 'grok'],
    gemini: () => [...queryKeys.oauth.all, 'gemini'],
    kimi:   () => [...queryKeys.oauth.all, 'kimi'],
    // One Zhipu connection serves either console (BigModel or Z.ai), so one key
    // answers for both provider ids.
    zhipu:  () => [...queryKeys.oauth.all, 'zhipu'],
    mimo:   () => [...queryKeys.oauth.all, 'mimo'],
    deepseek: () => [...queryKeys.oauth.all, 'deepseek'],
    deepseekModels: () => [...queryKeys.oauth.all, 'deepseek', 'models'],
    // The connection capability catalog: which cards the UI renders.
    providers: () => [...queryKeys.oauth.all, 'providers'],
  },
  workspaces: {
    all:    ['workspaces'],
    lists:  () => [...queryKeys.workspaces.all, 'list'],
    list:   (params: Record<string, unknown>) => [...queryKeys.workspaces.lists(), params],
    details: () => [...queryKeys.workspaces.all, 'detail'],
    detail: (id: string) => [...queryKeys.workspaces.details(), id],
    home:   () => [...queryKeys.workspaces.all, 'home'],
  },
  // The signed prefix the owner's report iframes load under, one per workspace
  // because the grant covers the whole workspace and nothing narrower. Its own
  // root, not under `workspaces`: every workspace invalidation would re-mint
  // it, and a new prefix reloads every open report.
  fileGrants: {
    all:       ['fileGrants'],
    workspace: (wsId: string) => [...queryKeys.fileGrants.all, wsId],
  },
  // Stable `/a/<code>` links for files and apps, grouped by workspace. A share
  // or stop rewrites the cached entries holding that code under `links`, then
  // re-reads the link's file list and the workspace's shared list.
  shareLinks: {
    all:         ['shareLinks'],
    byWorkspace: (wsId: string) => [...queryKeys.shareLinks.all, wsId],
    links:       (wsId: string) => [...queryKeys.shareLinks.byWorkspace(wsId), 'link'],
    // `key` is the normalized entry path for a file, the port for an app.
    link:        (wsId: string, kind: string, key: string) => [...queryKeys.shareLinks.links(wsId), kind, key],
    files:       (wsId: string, code: string) => [...queryKeys.shareLinks.byWorkspace(wsId), 'files', code],
    shared:      (wsId: string) => [...queryKeys.shareLinks.byWorkspace(wsId), 'shared'],
  },
  // What an `/s/` or `/a/` page resolves to. The visitor flag is part of the
  // question: the owner gets a different answer for the same code.
  share: {
    all:      ['share'],
    metadata: (code: string, viewer: string | null, asVisitor: boolean, path: string | null) =>
      [...queryKeys.share.all, 'metadata', code, viewer, asVisitor, path],
  },
  threads: {
    all:         ['threads'],
    byWorkspace: (wsId: string) => [...queryKeys.threads.all, 'workspace', wsId],
    // ThreadGallery's infinite list. Deliberately UNDER the byWorkspace prefix
    // so the lifecycle feed's prefix invalidation and the gallery's own
    // self-invalidations keep reaching it; the suffix keeps it distinct from
    // the sidebar's finite page entries, which cannot hold InfiniteData.
    gallery:     (wsId: string, archived: boolean) => [...queryKeys.threads.byWorkspace(wsId), { view: 'gallery', archived }],
    // The first `limit` rows, a finite list. The size is in the key because the
    // entry holds exactly that many rows, so a new size must miss the cache.
    page:        (wsId: string, limit: number) => [...queryKeys.threads.byWorkspace(wsId), limit, 0],
    detail:      (threadId: string) => [...queryKeys.threads.all, 'detail', threadId],
    provenance:  (threadId: string) => [...queryKeys.threads.all, 'provenance', threadId],
    // Base for every recent-list variant — invalidation targets this prefix.
    recentAll:   () => [...queryKeys.threads.all, 'recent'],
    recent:      (limit: number) => [...queryKeys.threads.recentAll(), limit],
    status:      (threadId: string) => [...queryKeys.threads.all, 'status', threadId],
    // Phase-3 shell read: the thread's goal/plan/task snapshot. One key per
    // thread, refreshed when a turn settles (the goal moves with the turn).
    goal:        (threadId: string) => [...queryKeys.threads.all, 'goal', threadId],
    // The refactor audit trail page for one thread (Phase-3 shell visibility).
    events:      (threadId: string) => [...queryKeys.threads.all, 'events', threadId],
    // The thread's D3 permission mode (flag-shaped read; one key per thread).
    permission:  (threadId: string) => [...queryKeys.threads.all, 'permission', threadId],
    // Batched dispatch-liveness read for a turn's PTC cards. The base key
    // targets every id-set variant for invalidation; the concrete key is stable
    // on the SORTED id set so registration order never churns the cache entry.
    dispatchLivenessAll: () => [...queryKeys.threads.all, 'dispatch-liveness'],
    dispatchLiveness: (ids: string[]) => [...queryKeys.threads.dispatchLivenessAll(), [...ids].sort()],
    // The same read keyed on the threads' run states as well, so a run that
    // starts or settles is a fresh read of how long each has been quiet.
    dispatchActivity: (ids: string[], revision: string) =>
      [...queryKeys.threads.dispatchLivenessAll(), 'activity', [...ids].sort(), revision],
  },
  workspaceFiles: {
    all:  ['workspaceFiles'],
    // Every listing and body of one workspace, for a change that rewrites
    // its whole folder (a restore from its backup).
    workspace: (wsId: string) => [...queryKeys.workspaceFiles.all, wsId],
    byWs: (wsId: string, opts?: Record<string, unknown>) => [...queryKeys.workspaceFiles.all, wsId, opts],
    // One file's bytes. `mode` rides in the key because the same path answers
    // in a different shape per reader — paginated text, full source, an
    // ArrayBuffer — and a viewer handed the wrong one renders nothing.
    // `scope` is the workspace id, or, for a share that has no workspace of
    // its own, the panel's mount id, so two shares never read each other's
    // bytes out of one cache. Deliberately under `all` but past the list's
    // options slot: dropping a workspace drops its bodies with it.
    bodies: (scope: string) => [...queryKeys.workspaceFiles.all, scope, 'body'],
    body: (scope: string, path: string, mode: string) => [...queryKeys.workspaceFiles.bodies(scope), path, mode],
  },
  memory: {
    all:       ['memory'],
    user:      () => [...queryKeys.memory.all, 'user'],
    userRead:  (key: string) => [...queryKeys.memory.user(), 'read', key],
    workspace: (wsId: string) => [...queryKeys.memory.all, 'workspace', wsId],
    workspaceRead: (wsId: string, key: string) => [...queryKeys.memory.workspace(wsId), 'read', key],
    // Refactor projection inspector (tier + ranked query); the variant is part
    // of the key so changing the filter is a different read.
    index:     (params: { tier?: string | null; q?: string; limit?: number }) =>
      [...queryKeys.memory.all, 'index', params],
    // The D5 proposal channel (read-only), flag-shaped like the index.
    proposals: (limit: number) => [...queryKeys.memory.all, 'proposals', limit],
    // The ontology view: the concepts the owner has memory for, and the
    // memories under one of them. The filters are part of the key, so a
    // different concept is a different read -- and so is the unnamed group,
    // which is a different selection from no selection at all.
    concepts: () => [...queryKeys.memory.all, 'concepts'],
    conceptEntries: (filters: {
      class?: string | null;
      about?: string | null;
      classIsNull?: boolean;
      aboutIsNull?: boolean;
      limit?: number;
    }) => [...queryKeys.memory.all, 'concept-entries', filters],
  },
  memo: {
    all:  ['memo'],
    list: () => [...queryKeys.memo.all, 'list'],
    read: (key: string) => [...queryKeys.memo.all, 'read', key],
  },
  mcp: {
    all:       ['mcp'],
    // The account's servers (not workspace-scoped).
    catalog:   () => [...queryKeys.mcp.all, 'catalog'],
    // Process-global builtins with the user's account-wide toggles.
    builtins:  () => [...queryKeys.mcp.all, 'builtins'],
    // Effective per-workspace server list (builtins + account servers).
    workspace: (wsId: string) => [...queryKeys.mcp.all, 'workspace', wsId],
    // Discovered tool snapshot for one catalog server (the detail view).
    serverTools: (name: string) => [...queryKeys.mcp.all, 'serverTools', name],
    // A builtin's tools, read from the frozen process registry. Its own family
    // rather than a child of `builtins()`: schemas are fixed for the process
    // lifetime, so a toggle there has nothing to tell this.
    builtinServerTools: (name: string) => [
      ...queryKeys.mcp.all,
      'builtinServerTools',
      name,
    ],
    // Every pre-save check. Under `mcp` so a vault mutation, which answers
    // `missing_secrets` for all of them, drops them with the fan-out.
    probes: () => [...queryKeys.mcp.all, 'probe'],
    // The add form's pre-save check of one address with one set of headers.
    // Both go in the key because the verdict is about the pair: the same URL
    // answers differently once a credential rides along. `headers` is a digest
    // of the map rather than the map itself: a key is cached state, and saying
    // which credential was in hand needs no more than that, while a row the
    // user is still filling in still never collides with the finished one.
    probe: (url: string, headers: string) => [
      ...queryKeys.mcp.probes(),
      url,
      headers,
    ],
  },
  // Skills are per-user and mutable; the mode variant is what the slash menu
  // reads, the manage variant is the full list including disabled rows. A
  // workspace id keys the workspace-effective view (shadowing + disables).
  skills: {
    all:  ['skills'],
    // The scope slot is one of: 'user' (user view), a workspace id
    // (workspace-effective view), or 'all-scopes' (the Plugins inventory) —
    // allScopes and workspaceId are mutually exclusive on the wire.
    list: (
      includeDisabled = false,
      workspaceId: string | null = null,
      allScopes = false,
    ) =>
      [
        ...queryKeys.skills.all, 'list', includeDisabled,
        allScopes ? 'all-scopes' : (workspaceId ?? 'user'),
      ],
    // One file of a skill, SKILL.md by default (the detail view).
    content: (name: string, workspaceId: string | null = null, path = 'SKILL.md') =>
      [...queryKeys.skills.all, 'content', name, workspaceId ?? 'user', path],
    // The server's upload caps; deployment configuration, so they hold for
    // the life of the page.
    limits: () => [...queryKeys.skills.all, 'limits'],
  },
  // One read carries every subagent plus what the pane needs to edit them
  // (connector names, limits), so a write lands in one entry.
  subagents: {
    all:  ['subagents'],
    list: () => [...queryKeys.subagents.all, 'list'],
  },
  userVault: {
    all:        ['userVault'],
    secrets:    () => [...queryKeys.userVault.all, 'secrets'],
    blueprints: () => [...queryKeys.userVault.all, 'blueprints'],
  },
  // Installed Agent Plugins packages. Their components live in the mcp and
  // skills caches; mutations here invalidate the whole fan-out.
  plugins: {
    all:    ['plugins'],
    list:   () => [...queryKeys.plugins.all, 'list'],
    detail: (name: string) => [...queryKeys.plugins.all, 'detail', name],
    // Under `all` so an install anywhere refreshes each entry's `installed`.
    marketplace: () => [...queryKeys.plugins.all, 'marketplace'],
  },
  // Runtime readiness diagnostics (`GET /api/v1/system/readiness`, answered
  // `Cache-Control: private, no-store`). The authenticated user id is part of
  // the key because the answer describes that user's configuration: it must
  // never be read by another account. Sign-out and account switch clear the
  // whole client (`AuthContext`'s `queryClient.clear()`), so no extra reset is
  // registered for it.
  system: {
    all:       ['system'],
    readiness: (userId: string | null) => [...queryKeys.system.all, 'readiness', userId ?? 'anonymous'],
  },
  // CN dashboard headlines. The domestic feed is shared; the global feed is
  // translated for the signed-in user, so it keys on the user id and can never
  // outlive them. `limit` rides in the key because the entry holds that many
  // rows.
  cnNews: {
    all:      ['cnNews'],
    domestic: (limit: number) => [...queryKeys.cnNews.all, 'domestic', limit],
    global:   (userId: string | null, limit: number) => [...queryKeys.cnNews.all, 'global', userId ?? 'anonymous', limit],
  },
};

/**
 * Marks a query as observed cache-only *by choice* — its arguments are complete
 * and its queryFn would succeed, it simply must not fetch on its own schedule.
 * Carrying it is what lets `refetchCacheOnlyLists`
 * (lib/threadLifecycle/feedClient.ts) fetch a query behind `enabled: false`.
 *
 * Never put it on a query that is disabled because an argument is missing:
 * those queryFns throw on the absent id, and the parked error is then read as a
 * real failure by whatever watches the query — which is how a thread lookup
 * with no id once evicted the user from every /chat route.
 */
export const CACHE_ONLY_META = { cacheOnly: true } as const;

/** Reader for {@link CACHE_ONLY_META} — keeps the flag's name in one module. */
export function isCacheOnlyMeta(meta: QueryMeta | undefined): boolean {
  return meta?.cacheOnly === true;
}
