import React, { Suspense } from 'react';
import { Routes, Route, Navigate, useLocation } from 'react-router';
import PageLoading from '@/components/PageLoading/PageLoading';
import { useIsMobile } from '@/hooks/useIsMobile';
import { useSyncUserLocale } from '@/hooks/useSyncUserLocale';
import { ContextOverflowPill } from '@/components/ui/ContextOverflowPill';
import NetworkBanner from '@/components/NetworkBanner/NetworkBanner';
import { StaleBuildBoundary } from '@/components/StaleBuildBoundary';
import { lazyWithMotion } from '@/lib/lazyWithMotion';

const InboxPage = lazyWithMotion(() => import('@/features/superagent/InboxPage'));
const TasksPage = lazyWithMotion(() => import('@/features/superagent/TasksPage'));
const TaskPage = lazyWithMotion(() => import('@/features/superagent/TaskPage'));
const SourcesPage = lazyWithMotion(() => import('@/features/superagent/SourcesPage'));
const AgentsPage = lazyWithMotion(() => import('@/features/superagent/AgentsPage'));
const LinesPage = lazyWithMotion(() => import('@/features/superagent/LinesPage'));
const DispatchListPage = lazyWithMotion(() => import('@/features/dispatch/DispatchListPage'));
const DispatchDetailPage = lazyWithMotion(() => import('@/features/dispatch/DispatchDetailPage'));
const AgentFleetPage = lazyWithMotion(() => import('@/features/agentfleet/AgentFleetPage'));
const SkillsMcpPage = lazyWithMotion(() => import('@/features/skills-mcp/SkillsMcpPage'));
const RunsPage = lazyWithMotion(() => import('@/features/superagent/RunsPage'));
const Plugins = lazyWithMotion(() => import('../../pages/Plugins/Plugins'));
const Settings = lazyWithMotion(() => import('../../pages/Settings/Settings'));
const Memory = lazyWithMotion(() => import('../../pages/Memory/Memory'));

/** Permanent alias for the pre-rename Connectors page. Keeps search + hash:
 * OAuth `return_to` values persisted before the rename (Redis ConnectState,
 * 600s TTL) still land on `/connectors?mcp_connected=…` and must reach the
 * page that renders the toast. */
export function LegacyConnectorsRedirect() {
  const { search, hash } = useLocation();
  return <Navigate to={`/plugins${search}${hash}`} replace />;
}

function Main() {
  const location = useLocation();
  const isMobile = useIsMobile();
  useSyncUserLocale();
  // Key by top-level path segment so /chat sub-routes share a key (no re-animation)
  const pageKey = location.pathname.split('/')[1] || 'dashboard';

  // A chunk a deploy deleted rejects rather than stays pending, so Suspense
  // hands the throw straight up; without a boundary it reaches the root and
  // takes the sidebar with it, and the pane's spinner hangs forever.
  //
  // Inside Suspense, not around it, even though both placements catch the
  // rejection (pinned in src/lib/__tests__/staleBuild.test.tsx). The boundary is keyed by
  // route so navigating away from a dead chunk clears the error, and a key on
  // the outside remounts Suspense along with it. Desktop already remounts this
  // subtree through the keyed fade wrapper, but mobile renders it directly, and a
  // freshly mounted Suspense boundary has no previous content to hold — so it
  // must show its fallback, and with the router's transition-wrapped updates
  // every first navigation to a route flashed the pane spinner where it used to
  // switch in place.
  const routes = (
    <Suspense fallback={<PageLoading variant="pane" />}>
      <StaleBuildBoundary key={pageKey} variant="pane">
        <Routes location={location}>
          <Route path="/" element={<Navigate to="/inbox" replace />} />
          <Route path="/inbox" element={<InboxPage />} />
          <Route path="/tasks" element={<TasksPage />} />
          <Route path="/tasks/:ticketId" element={<TaskPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/lines" element={<LinesPage />} />
          <Route path="/dispatch" element={<DispatchListPage />} />
          <Route path="/dispatch/:dispatchId" element={<DispatchDetailPage />} />
          {/* The local worktree fleet is a property of the host, so it reads as
              a peer of the agent inventory rather than a sub-page of it. */}
          <Route path="/agent-fleet" element={<AgentFleetPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/sources" element={<SourcesPage />} />
          <Route path="/plugins" element={<Plugins />} />
          <Route path="/skills-mcp" element={<SkillsMcpPage />} />
          <Route path="/connectors" element={<LegacyConnectorsRedirect />} />
          <Route path="/settings" element={<Settings />} />
          {/* Memory is browsed by concept: what kind of claim each memory is,
              and the thing it is about. */}
          <Route path="/memory" element={<Memory />} />
          {/* Retired product surfaces. Deep links keep working by landing in
              the one inbox; their old pages are removed from the shell. */}
          <Route path="/superagent" element={<Navigate to="/inbox" replace />} />
          <Route path="/chat/*" element={<Navigate to="/inbox" replace />} />
          <Route path="/projects/*" element={<Navigate to="/inbox" replace />} />
          <Route path="/automations" element={<Navigate to="/inbox" replace />} />
          <Route path="*" element={<Navigate to="/inbox" replace />} />
        </Routes>
      </StaleBuildBoundary>
    </Suspense>
  );

  // On mobile, skip the fade: instant page switches feel snappier. The
  // wrapper is not cosmetic symmetry with the desktop branch below: it carries
  // the same `minHeight: 0`, which is the only thing that lets the route shrink
  // when the banner takes part of the column. Without it a route root pinned
  // with `min-height: 100%` (the dashboard) keeps the full column height and
  // its last banner's-worth of scroll area slides under the bottom tab bar —
  // invisible to an `.app-main` overflow check, because `.app-main` reserves
  // exactly that strip as padding for the tab bar. It must also stay a flex
  // column: the mobile dashboard is `height: auto` and relies on being a flex
  // item for its height, so a plain block wrapper would let it grow to its full
  // scroll height instead.
  if (isMobile) {
    return (
      <div className="main" style={{ height: '100%' }}>
        <NetworkBanner />
        <div className="route-stage route-stage--mobile">
          {routes}
        </div>
        <ContextOverflowPill />
      </div>
    );
  }

  return (
    <div className="main">
      <NetworkBanner />
      {/* Keyed on the section so a new one mounts fresh and fades in. */}
      <div key={pageKey} className="route-fade-in route-stage">
        {routes}
      </div>
      <ContextOverflowPill />
    </div>
  );
}

export default Main;
