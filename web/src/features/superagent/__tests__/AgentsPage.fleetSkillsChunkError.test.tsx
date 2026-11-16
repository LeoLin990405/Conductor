import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';

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

// A deploy can delete the panel's chunk while a tab still holds the old
// document; the lazy import then rejects. The page must survive that as an
// inline notice in the fleet-skills section, not lose the whole Agents page
// to the route-level boundary.
vi.mock('../FleetSkillsPanel', async () => {
  throw new Error('chunk missing after deploy');
});

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
  apiMocks.getNativeAuthStatuses.mockResolvedValue([]);
  apiMocks.getAutonomyMetrics.mockResolvedValue({ running_tickets: 0, max_workers: 4 });
  apiMocks.listAutonomyDaemons.mockResolvedValue({ daemons: [] });
  apiMocks.getMainAgentStatusBoard.mockResolvedValue({
    agents: [],
    alerts: [],
    thresholds: { heartbeat_seconds: 60, progress_seconds: 300 },
  });
});

describe('AgentsPage fleet skills chunk failure', () => {
  it('keeps the rest of the page when the panel chunk fails to load', async () => {
    renderPage();
    expect(screen.getByText('Main Agent status')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Show fleet skills' }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Fleet skills failed to load');
    // The page around the section is untouched.
    expect(screen.getByText('Main Agent status')).toBeInTheDocument();
    expect(screen.getByText('Local daemons')).toBeInTheDocument();
  });
});
