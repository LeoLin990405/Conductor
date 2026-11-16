import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';

const apiMocks = vi.hoisted(() => ({
  listAutonomySources: vi.fn(),
  listAutonomyTickets: vi.fn(),
  listAutonomyDaemons: vi.fn(),
  createAutonomyDaemon: vi.fn(),
  updateAutonomyDaemon: vi.fn(),
  deleteAutonomyDaemon: vi.fn(),
  getAutonomyMetrics: vi.fn(),
  createAutonomySource: vi.fn(),
  updateAutonomySource: vi.fn(),
  checkAutonomySource: vi.fn(),
  pollAutonomySource: vi.fn(),
  deleteAutonomySource: vi.fn(),
  listAutonomySchedules: vi.fn(),
  createAutonomySchedule: vi.fn(),
  updateAutonomySchedule: vi.fn(),
  deleteAutonomySchedule: vi.fn(),
  runAutonomySchedule: vi.fn(),
  getUnavailableEvals: vi.fn(),
  requeueEval: vi.fn(),
  getAutonomyPolicy: vi.fn(),
  updateAutonomyPolicy: vi.fn(),
}));

vi.mock('@/api/autonomy', () => apiMocks);
vi.mock('@/hooks/useAllWorkspaces', () => ({
  useAllWorkspaces: () => ({ data: { workspaces: [{ workspace_id: 'ws-1', name: 'Project' }] } }),
}));
vi.mock('@/hooks/useNativeAgents', () => ({ useNativeAgents: () => ({ data: [] }) }));
vi.mock('@/api/nativeAgents', () => ({ getNativeAuthStatuses: vi.fn().mockResolvedValue([]) }));

import { SchedulePanel, SourcesPanel } from '../SourcesPage';
import AgentsPage from '../AgentsPage';

const daemon = {
  daemon_id: 'daemon-1', name: 'Leo laptop', provider: 'codex', max_concurrency: 1, last_seen_at: null, created_at: '2026-11-02T00:00:00Z',
};

function renderWithQuery(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMocks.listAutonomySources.mockResolvedValue({ sources: [] });
  apiMocks.listAutonomyTickets.mockResolvedValue({ tickets: [] });
  apiMocks.listAutonomyDaemons.mockResolvedValue({ daemons: [daemon] });
  apiMocks.getAutonomyMetrics.mockResolvedValue({ running_tickets: 0, max_workers: 4 });
  apiMocks.createAutonomyDaemon.mockResolvedValue({ ...daemon, token: 'one-time-token' });
  apiMocks.updateAutonomyDaemon.mockImplementation(async (_daemonId, input) => ({ ...daemon, ...input }));
  apiMocks.createAutonomySource.mockResolvedValue({ source_id: 'source-new' });
  apiMocks.updateAutonomySource.mockResolvedValue({ source_id: 'source-1' });
  apiMocks.checkAutonomySource.mockResolvedValue({ ok: true });
  apiMocks.listAutonomySchedules.mockResolvedValue({ schedules: [] });
  apiMocks.createAutonomySchedule.mockResolvedValue({ schedule_id: 'schedule-new' });
  apiMocks.updateAutonomySchedule.mockResolvedValue({ schedule_id: 'schedule-1' });
});

describe('local daemon concurrency settings', () => {
  it('creates a daemon with the selected concurrency', async () => {
    const user = userEvent.setup();
    renderWithQuery(<AgentsPage />);

    await user.type(await screen.findByLabelText('Daemon name'), 'workstation');
    await user.clear(screen.getByLabelText('Max parallel Tickets'));
    await user.type(screen.getByLabelText('Max parallel Tickets'), '3');
    await user.click(screen.getByRole('button', { name: 'Create daemon token' }));

    await waitFor(() => expect(apiMocks.createAutonomyDaemon).toHaveBeenCalledWith({
      name: 'workstation', provider: 'codex', max_concurrency: 3,
    }));
  });

  it('saves a daemon capacity change through the API', async () => {
    const user = userEvent.setup();
    renderWithQuery(<AgentsPage />);

    const input = await screen.findByLabelText('Max parallel Tickets for Leo laptop');
    await user.clear(input);
    await user.type(input, '4');
    await user.click(screen.getByRole('button', { name: 'Save concurrency' }));

    await waitFor(() => expect(apiMocks.updateAutonomyDaemon).toHaveBeenCalledWith('daemon-1', {
      max_concurrency: 4,
    }));
  });
});

describe('local daemon routing for Sources and schedules', () => {
  it.each([
    ['github', 'Repository', 'owner/repo'],
    ['feishu', 'Feishu chat id', 'oc_chat'],
  ] as const)('creates a %s Source with its selected daemon', async (kind, field, value) => {
    const user = userEvent.setup();
    renderWithQuery(<SourcesPanel />);

    await user.selectOptions(screen.getByLabelText('Source kind'), kind);
    await user.type(screen.getByLabelText(field), value);
    await user.selectOptions(screen.getByLabelText('Workspace'), 'ws-1');
    await user.selectOptions(screen.getByLabelText('Run Tickets on'), 'daemon-1');
    expect(screen.getByText('The selected daemon provides the Worker runtime.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Add Source' }));

    await waitFor(() => expect(apiMocks.createAutonomySource).toHaveBeenCalledWith(expect.objectContaining({
      kind,
      config: expect.objectContaining({ workspace_id: 'ws-1', worker_daemon_id: 'daemon-1' }),
    })));
  });

  it('keeps an edited Source daemon selected and sends null when cleared', async () => {
    const user = userEvent.setup();
    apiMocks.listAutonomySources.mockResolvedValue({
      sources: [{
        source_id: 'source-1', kind: 'github', enabled: true,
        config: {
          repository: 'owner/repo', secret_name: 'GITHUB_TOKEN', workspace_id: 'ws-1',
          allowed_logins: [], poll_seconds: 60, worker_daemon_id: 'daemon-1', worker_provider: 'codex',
        },
        state: {},
      }],
    });
    renderWithQuery(<SourcesPanel />);

    await user.click(await screen.findByRole('button', { name: 'Edit' }));
    expect(screen.getByLabelText('Run Tickets on')).toHaveValue('daemon-1');
    expect(screen.getByLabelText('Worker agent')).toHaveValue('codex');
    await user.selectOptions(screen.getByLabelText('Run Tickets on'), '');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(apiMocks.updateAutonomySource).toHaveBeenCalledWith('source-1', {
      config: expect.objectContaining({ worker_daemon_id: null }),
    }));
  });

  it('creates a cron schedule bound to the selected daemon', async () => {
    const user = userEvent.setup();
    renderWithQuery(<SchedulePanel />);

    await user.type(screen.getByLabelText('Prompt'), 'Review the project');
    await user.selectOptions(screen.getByLabelText('Workspace'), 'ws-1');
    await user.selectOptions(screen.getByLabelText('Run Tickets on'), 'daemon-1');
    expect(screen.getByText('The selected daemon provides the Worker runtime.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Create schedule' }));

    await waitFor(() => expect(apiMocks.createAutonomySchedule).toHaveBeenCalledWith(expect.objectContaining({
      workspace_id: 'ws-1', worker_daemon_id: 'daemon-1', prompt: 'Review the project',
    })));
  });

  it('preserves an unavailable daemon in the schedule editor until the user clears it', async () => {
    const user = userEvent.setup();
    apiMocks.listAutonomySchedules.mockResolvedValue({
      schedules: [{
        schedule_id: 'schedule-1', title: 'Daily', prompt: 'Review', workspace_id: 'ws-1',
        cron_expression: '0 9 * * 1-5', timezone: 'UTC', worker_provider: 'codex', worker_daemon_id: 'gone-1234',
        enabled: true, next_run_at: '2026-11-03T09:00:00Z', last_run_at: null,
      }],
    });
    renderWithQuery(<SchedulePanel />);

    await user.click(await screen.findByRole('button', { name: 'Edit' }));
    expect(screen.getByLabelText('Run Tickets on')).toHaveValue('gone-1234');
    expect(screen.getByRole('option', { name: 'Unavailable local daemon · gone-123' })).toBeDisabled();
    await user.selectOptions(screen.getByLabelText('Run Tickets on'), '');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(apiMocks.updateAutonomySchedule).toHaveBeenCalledWith('schedule-1', expect.objectContaining({
      worker_daemon_id: null,
    })));
  });
});
