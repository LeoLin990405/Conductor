import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import i18n from 'i18next';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { assertNoNamespaceCollision, assertParity } from '@/lib/featureLocales';
import tnEn from '../locales/tnAgents.en-US.json';
import tnZh from '../locales/tnAgents.zh-CN.json';

const apiMocks = vi.hoisted(() => ({
  getNativeAuthStatuses: vi.fn(),
  getMainAgentStatusBoard: vi.fn(),
  createAutonomyDaemon: vi.fn(),
  deleteAutonomyDaemon: vi.fn(),
  getAutonomyMetrics: vi.fn(),
  listAutonomyDaemons: vi.fn(),
}));

vi.mock('@/api/nativeAgents', () => ({ getNativeAuthStatuses: apiMocks.getNativeAuthStatuses }));
vi.mock('@/api/mainAgent', () => ({ getMainAgentStatusBoard: apiMocks.getMainAgentStatusBoard }));
vi.mock('@/api/autonomy', () => ({
  createAutonomyDaemon: apiMocks.createAutonomyDaemon,
  deleteAutonomyDaemon: apiMocks.deleteAutonomyDaemon,
  getAutonomyMetrics: apiMocks.getAutonomyMetrics,
  listAutonomyDaemons: apiMocks.listAutonomyDaemons,
}));
const nativeAgents = vi.hoisted(() => ({ data: [] as unknown[] }));
vi.mock('@/hooks/useNativeAgents', () => ({ useNativeAgents: () => nativeAgents }));

import AgentsPage from '../AgentsPage';

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter><AgentsPage /></MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  nativeAgents.data = [];
  apiMocks.getNativeAuthStatuses.mockResolvedValue([]);
  apiMocks.getAutonomyMetrics.mockResolvedValue({ running_tickets: 0, max_workers: 4 });
  apiMocks.listAutonomyDaemons.mockResolvedValue({ daemons: [] });
  apiMocks.createAutonomyDaemon.mockResolvedValue({ daemon_id: 'd', token: 'token' });
  apiMocks.deleteAutonomyDaemon.mockResolvedValue({});
});

describe('AgentsPage Main Agent status board', () => {
  it('shows active workers, heartbeat, progress, stall reason, and alerts', async () => {
    apiMocks.getMainAgentStatusBoard.mockResolvedValue({
      agents: [{
        run_id: 'run-1', task_id: 'task-1', ticket_id: 'ticket-1', agent_id: 'worker-a', daemon_id: 'daemon-a', state: 'running',
        last_heartbeat: '2026-11-02T01:00:00Z', current_step: 'Reviewing files', stalled: true, stall_reason: 'No progress for 5 minutes',
      }, {
        run_id: 'run-1', task_id: 'task-2', ticket_id: 'ticket-1', agent_id: 'worker-b', daemon_id: null, state: 'running',
        last_heartbeat: '2026-11-02T01:00:01Z', current_step: 'Checking tests', stalled: false, stall_reason: null,
      }],
      alerts: [{ id: 'alert-1', run_id: 'run-1', task_id: 'task-1', severity: 'warning', message: 'Worker heartbeat expired', created_at: '2026-11-02T01:00:00Z' }],
      thresholds: { heartbeat_seconds: 60, progress_seconds: 300 },
    });
    renderPage();

    expect(await screen.findByText('Reviewing files')).toBeInTheDocument();
    expect(screen.getByText('Checking tests')).toBeInTheDocument();
    expect(screen.getByText('No progress for 5 minutes')).toBeInTheDocument();
    expect(screen.getByText(/worker-a/)).toBeInTheDocument();
    expect(screen.getByText(/Worker heartbeat expired/)).toBeInTheDocument();
    expect(screen.getByText(/heartbeat 60s · progress 300s/)).toBeInTheDocument();
    expect(apiMocks.getMainAgentStatusBoard).toHaveBeenCalledTimes(1);
  });

  it('shows an empty state when no runs or alerts exist', async () => {
    apiMocks.getMainAgentStatusBoard.mockResolvedValue({ agents: [], alerts: [], thresholds: { heartbeat_seconds: 60, progress_seconds: 300 } });
    renderPage();
    expect(await screen.findByText('No Main Agent runs are active.')).toBeInTheDocument();
    expect(screen.getByText('No alerts.')).toBeInTheDocument();
  });

  it('shows an error state when the board cannot be read', async () => {
    apiMocks.getMainAgentStatusBoard.mockRejectedValue(new Error('offline'));
    renderPage();
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not read Main Agent status. Try Refresh.');
  });
});

const RUNS = {
  agents: [{
    run_id: 'run-1', task_id: 'task-1', ticket_id: 'ticket-1', agent_id: 'worker-a', daemon_id: 'daemon-a', state: 'running',
    last_heartbeat: '2026-11-02T01:00:00Z', current_step: 'Reviewing files', stalled: true, stall_reason: 'No progress for 5 minutes',
  }, {
    run_id: 'run-1', task_id: 'task-2', ticket_id: null, agent_id: 'worker-b', daemon_id: 'daemon-b', state: 'running',
    last_heartbeat: null, current_step: '', stalled: false, stall_reason: null,
  }],
  alerts: [],
  thresholds: { heartbeat_seconds: 60, progress_seconds: 300 },
};

describe('AgentsPage terminal-native rows', () => {
  it('marks each Main Agent run with a state glyph and word', async () => {
    apiMocks.getMainAgentStatusBoard.mockResolvedValue(RUNS);
    renderPage();
    const stalled = await screen.findByTestId('main-agent-run-run-1-task-1');
    const busy = screen.getByTestId('main-agent-run-run-1-task-2');
    expect(stalled.querySelector('[data-state]')?.getAttribute('data-state')).toBe('stalled');
    expect(within(stalled).getByText('stalled')).toBeInTheDocument();
    expect(stalled.textContent).toContain('✖');
    expect(busy.querySelector('[data-state]')?.getAttribute('data-state')).toBe('busy');
    expect(within(busy).getByText('busy')).toBeInTheDocument();
    expect(within(busy).getByText('Waiting for progress')).toBeInTheDocument();
    expect(within(busy).getByText(/Not reported/)).toBeInTheDocument();
  });

  it('shows the PTC agent online and a native runtime online or offline by its proof', async () => {
    nativeAgents.data = [
      { provider: 'codex', installed: true, live_verified: true },
      { provider: 'kimi', installed: true, live_verified: false },
    ];
    apiMocks.getNativeAuthStatuses.mockResolvedValue([{ provider: 'codex', authenticated: true }]);
    apiMocks.getMainAgentStatusBoard.mockResolvedValue(RUNS);
    renderPage();
    const ptc = screen.getByTestId('agent-row-ptc');
    expect(within(ptc).getByText('online')).toBeInTheDocument();
    const codex = screen.getByTestId('agent-row-codex');
    const kimi = screen.getByTestId('agent-row-kimi');
    expect(codex.querySelector('[data-state]')?.getAttribute('data-state')).toBe('online');
    expect(kimi.querySelector('[data-state]')?.getAttribute('data-state')).toBe('offline');
    expect(await within(codex).findByText('signed in')).toBeInTheDocument();
    expect(within(kimi).getByText('sign-in needed')).toBeInTheDocument();
    expect(within(kimi).getByRole('button', { name: 'Create task' })).toBeDisabled();
    expect(within(codex).getByRole('button', { name: 'Create task' })).toBeEnabled();
  });

  it('marks a daemon busy while the board shows a run on it', async () => {
    apiMocks.getMainAgentStatusBoard.mockResolvedValue(RUNS);
    apiMocks.listAutonomyDaemons.mockResolvedValue({
      daemons: [
        { daemon_id: 'daemon-a', name: 'busy box', provider: 'codex', max_concurrency: 1, last_seen_at: null, created_at: '2026-11-02T00:00:00Z' },
        { daemon_id: 'daemon-z', name: 'idle box', provider: '', max_concurrency: 1, last_seen_at: null, created_at: '2026-11-02T00:00:00Z' },
      ],
    });
    renderPage();
    await screen.findByText('Reviewing files');
    const busy = await screen.findByTestId('daemon-row-daemon-a');
    expect(busy.querySelector('[data-state]')?.getAttribute('data-state')).toBe('busy');
    const idle = screen.getByTestId('daemon-row-daemon-z');
    expect(idle.querySelector('[data-state]')?.getAttribute('data-state')).toBe('offline');
    expect(idle.textContent).toContain('native · never seen');
  });

  it('renders the page in zh-CN from the tnAgents catalog', async () => {
    apiMocks.getMainAgentStatusBoard.mockResolvedValue(RUNS);
    await i18n.changeLanguage('zh-CN');
    try {
      renderPage();
      expect(screen.getByRole('heading', { level: 1, name: 'Agent' })).toBeInTheDocument();
      expect(screen.getByRole('heading', { name: 'Main Agent 状态' })).toBeInTheDocument();
      expect(await screen.findByText('停滞')).toBeInTheDocument();
      expect(screen.getByText(/心跳 60 秒/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '显示舰队技能' })).toBeInTheDocument();
    } finally {
      await i18n.changeLanguage('en-US');
    }
  });
});

describe('daemon presence', () => {
  it('is online when seen within the heartbeat threshold and offline when older', async () => {
    apiMocks.getMainAgentStatusBoard.mockResolvedValue({ ...RUNS, agents: [] });
    apiMocks.listAutonomyDaemons.mockResolvedValue({
      daemons: [
        { daemon_id: 'fresh', name: 'fresh box', provider: 'codex', max_concurrency: 1, last_seen_at: new Date(Date.now() - 5_000).toISOString(), created_at: '2026-11-02T00:00:00Z' },
        { daemon_id: 'stale', name: 'stale box', provider: 'codex', max_concurrency: 1, last_seen_at: new Date(Date.now() - 10 * 60_000).toISOString(), created_at: '2026-11-02T00:00:00Z' },
      ],
    });
    renderPage();
    const fresh = await screen.findByTestId('daemon-row-fresh');
    expect(fresh.querySelector('[data-state]')?.getAttribute('data-state')).toBe('online');
    expect(within(fresh).getByText('online')).toBeInTheDocument();
    const stale = screen.getByTestId('daemon-row-stale');
    expect(stale.querySelector('[data-state]')?.getAttribute('data-state')).toBe('offline');
    expect(stale.textContent).toContain('seen');
  });
});

describe('tnAgents locales', () => {
  it('keeps en-US / zh-CN key parity', () => {
    expect(assertParity(tnEn, tnZh)).toEqual({ enOnly: [], zhOnly: [] });
  });

  it('tnAgents does not collide with the main catalog', () => {
    // Read the catalog from disk: the imported JSON module is the object i18n
    // deep-merges feature bundles into at runtime, so it already holds tnAgents.
    for (const file of ['en-US', 'zh-CN']) {
      const main = JSON.parse(readFileSync(resolve(process.cwd(), `src/locales/${file}.json`), 'utf8')) as Record<string, unknown>;
      expect(() => assertNoNamespaceCollision('tnAgents', main)).not.toThrow();
    }
    expect(Object.keys(tnEn)).toEqual(['tnAgents']);
    expect(Object.keys(tnZh)).toEqual(['tnAgents']);
  });
});
