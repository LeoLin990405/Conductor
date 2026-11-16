import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import i18n from 'i18next';

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
vi.mock('@/hooks/useNativeAgents', () => ({ useNativeAgents: () => ({ data: [] }) }));
vi.mock('@/hooks/useLocale', () => ({ useLocale: () => 'en' }));
vi.mock('@/lib/nativeRuntimeLabels', () => ({ nativeRuntimeLabel: (p: string) => p }));

const fleetMocks = vi.hoisted(() => ({
  getFleetSkillsInventory: vi.fn(),
}));
vi.mock('@/api/fleetSkills', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getFleetSkillsInventory: fleetMocks.getFleetSkillsInventory,
}));

import { ApiError } from '@/api/client';
import AgentsPage, { FleetSkillsPanelBoundary } from '../AgentsPage';

const inventory = {
  repo: '/home/me/.conductor/skills',
  skills: [
    {
      name: 'fleet-rules',
      description: 'Non-negotiable rules',
      installed: ['zcode', 'shared'],
    },
  ],
  agents: {
    zcode: {
      dir: '/home/me/.zcode/skills',
      exists: true,
      entries: [
        { name: 'fleet-rules', kind: 'symlink', managed: true, healthy: true },
        { name: 'handmade', kind: 'copy', managed: false, healthy: true },
      ],
      foreign: ['handmade'],
      broken_symlinks: [],
    },
    claude: {
      dir: '/home/me/.claude/skills',
      exists: false,
      entries: [],
      foreign: [],
      broken_symlinks: [],
    },
  },
  presets: [{ name: 'core', skills: ['fleet-rules'], description: '' }],
  projects: [{ project: 'conductor', lines: ['default', 'r2'] }],
};

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
  apiMocks.getNativeAuthStatuses.mockResolvedValue([]);
  apiMocks.getAutonomyMetrics.mockResolvedValue({ running_tickets: 0, max_workers: 4 });
  apiMocks.listAutonomyDaemons.mockResolvedValue({ daemons: [] });
  apiMocks.getMainAgentStatusBoard.mockResolvedValue({
    agents: [],
    alerts: [],
    thresholds: { heartbeat_seconds: 60, progress_seconds: 300 },
  });
  fleetMocks.getFleetSkillsInventory.mockResolvedValue(inventory);
});

describe('AgentsPage fleet skills section', () => {
  it('is collapsed until the operator opens it', async () => {
    renderPage();
    expect(screen.getByRole('button', { name: 'Show fleet skills' })).toBeTruthy();
    // the inventory query only runs once the panel mounts
    expect(fleetMocks.getFleetSkillsInventory).not.toHaveBeenCalled();
  });

  it('renders per-agent entries with managed/foreign markers', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: 'Show fleet skills' }));
    const zcode = await screen.findByTestId('fleet-skills-agent-zcode');
    expect(zcode.textContent).toContain('fleet-rules');
    expect(zcode.textContent).toContain('managed');
    expect(zcode.textContent).toContain('handmade');
    expect(zcode.textContent).toContain('foreign');
  });

  it('reports an unavailable inventory instead of guessing', async () => {
    // FastAPI answers with a plain-string `detail`; the panel must surface
    // that sentence, not the transport's "Request failed with status code N".
    fleetMocks.getFleetSkillsInventory.mockRejectedValue(
      new ApiError('Request failed with status code 503', 'ERR_BAD_RESPONSE', {
        data: { detail: 'repo unreadable' },
        status: 503,
        statusText: 'Service Unavailable',
        headers: {},
      }),
    );
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: 'Show fleet skills' }));
    const error = await screen.findByTestId('fleet-skills-error');
    expect(error.textContent).toContain('repo unreadable');
    expect(error.textContent).not.toContain('Request failed with status code');
  });

  it('surfaces a per-agent read error without hiding the other agents', async () => {
    fleetMocks.getFleetSkillsInventory.mockResolvedValue({
      ...inventory,
      agents: {
        ...inventory.agents,
        claude: {
          dir: '/home/me/.claude/skills',
          exists: true,
          entries: [],
          foreign: [],
          broken_symlinks: [],
          error: 'could not read directory: Permission denied',
        },
      },
    });
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: 'Show fleet skills' }));
    const claude = await screen.findByTestId('fleet-skills-agent-claude');
    expect(claude.textContent).toContain('could not read directory');
    const zcode = screen.getByTestId('fleet-skills-agent-zcode');
    expect(zcode.textContent).toContain('fleet-rules');
  });
});

describe('AgentsPage fleet skills in zh-CN', () => {
  it('labels entries and the toggle from the tnAgents catalog', async () => {
    await i18n.changeLanguage('zh-CN');
    try {
      renderPage();
      await userEvent.click(screen.getByRole('button', { name: '显示舰队技能' }));
      const zcode = await screen.findByTestId('fleet-skills-agent-zcode');
      expect(zcode.textContent).toContain('受管');
      expect(zcode.textContent).toContain('外部');
      const claude = screen.getByTestId('fleet-skills-agent-claude');
      expect(claude.textContent).toContain('（目录尚不存在）');
      expect(screen.getByRole('button', { name: '隐藏舰队技能' })).toHaveAttribute('aria-expanded', 'true');
    } finally {
      await i18n.changeLanguage('en-US');
    }
  });

  it('marks managed entries with the ok tag and foreign ones idle', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: 'Show fleet skills' }));
    const zcode = await screen.findByTestId('fleet-skills-agent-zcode');
    expect(zcode.querySelector('.ag-tag--ok')?.textContent).toBe('managed');
    expect(zcode.querySelector('.ag-tag--idle')?.textContent).toBe('foreign');
  });
});

describe('FleetSkillsPanelBoundary', () => {
  it('absorbs a render failure into an inline alert', () => {
    function Thrower(): never {
      throw new Error('panel exploded');
    }
    render(
      <FleetSkillsPanelBoundary>
        <Thrower />
      </FleetSkillsPanelBoundary>,
    );
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('Fleet skills failed to load');
  });

  it('retries the panel when the operator asks it to', async () => {
    let shouldThrow = true;
    function MaybeThrower() {
      if (shouldThrow) throw new Error('panel exploded');
      return <p>panel ok</p>;
    }
    render(
      <FleetSkillsPanelBoundary>
        <MaybeThrower />
      </FleetSkillsPanelBoundary>,
    );
    expect(screen.getByRole('alert')).toBeTruthy();
    shouldThrow = false;
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(screen.getByText('panel ok')).toBeTruthy();
  });
});
