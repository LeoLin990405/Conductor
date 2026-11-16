import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import i18n from '@/i18n';
import enUSRaw from '@/locales/en-US.json?raw';
import { assertNoNamespaceCollision, assertParity } from '@/lib/featureLocales';
import en from '../locales/tnSources.en-US.json';
import zh from '../locales/tnSources.zh-CN.json';

const apiMocks = vi.hoisted(() => ({
  listAutonomySources: vi.fn(),
  listAutonomyDaemons: vi.fn(),
  listAutonomyTickets: vi.fn(),
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

import SourcesPage, { SchedulePanel, SourcesPanel } from '../SourcesPage';

const daemon = {
  daemon_id: 'daemon-1', name: 'Leo laptop', provider: 'codex', max_concurrency: 1, last_seen_at: null, created_at: '2026-11-02T00:00:00Z',
};

const source = (overrides: Record<string, unknown> = {}) => ({
  source_id: 'source-ok', user_id: 'u', kind: 'github', enabled: true,
  config: {
    repository: 'owner/healthy', secret_name: 'GITHUB_TOKEN', workspace_id: 'ws-1',
    allowed_logins: [], poll_seconds: 60, worker_provider: 'codex', worker_daemon_id: 'daemon-1',
  },
  state: { last_poll_at: '2026-11-13T10:00:00Z', agent_login: 'conductor-bot' },
  created_at: '', updated_at: '',
  ...overrides,
});

const SOURCES = [
  source(),
  source({ source_id: 'source-err', config: { ...source().config, repository: 'owner/broken' }, state: { last_error: 'HTTP 401 bad credentials', last_poll_at: '2026-11-13T10:00:00Z' } }),
  source({ source_id: 'source-off', enabled: false, config: { ...source().config, repository: 'owner/paused' } }),
  source({ source_id: 'source-new', config: { ...source().config, repository: 'owner/fresh' }, state: {} }),
];

const ticketsFor: Record<string, Array<{ status: string; attention: unknown }>> = {
  'source-ok': [
    { status: 'running', attention: null },
    { status: 'queued', attention: null },
    { status: 'blocked', attention: { question: 'merge?' } },
    { status: 'failed', attention: null },
    { status: 'completed', attention: null },
  ],
};

function renderWithQuery(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const rowOf = (name: string) => screen.getByText(name).closest('li') as HTMLElement;

beforeEach(() => {
  vi.clearAllMocks();
  apiMocks.listAutonomySources.mockResolvedValue({ sources: [] });
  apiMocks.listAutonomyDaemons.mockResolvedValue({ daemons: [daemon] });
  apiMocks.listAutonomyTickets.mockImplementation(async (filter: { source_id?: string; schedule_id?: string }) => ({
    tickets: ticketsFor[filter.source_id ?? filter.schedule_id ?? ''] ?? [],
  }));
  apiMocks.createAutonomySource.mockResolvedValue({ source_id: 'source-created' });
  apiMocks.updateAutonomySource.mockResolvedValue({ source_id: 'source-ok' });
  apiMocks.checkAutonomySource.mockResolvedValue({ ok: true, agent_login: 'conductor-bot', can_push: false });
  apiMocks.pollAutonomySource.mockResolvedValue({ source_id: 'source-ok' });
  apiMocks.deleteAutonomySource.mockResolvedValue(undefined);
  apiMocks.listAutonomySchedules.mockResolvedValue({ schedules: [] });
  apiMocks.createAutonomySchedule.mockResolvedValue({ schedule_id: 'schedule-new' });
  apiMocks.updateAutonomySchedule.mockResolvedValue({ schedule_id: 'schedule-1' });
  apiMocks.getUnavailableEvals.mockResolvedValue({ evals: [] });
  apiMocks.getAutonomyPolicy.mockResolvedValue({ accept_mode: 'explicit', admission: {} });
});

afterEach(async () => {
  vi.restoreAllMocks();
  await i18n.changeLanguage('en-US');
});

describe('tnSources locales', () => {
  it('keeps en-US / zh-CN key parity', () => {
    expect(assertParity(en, zh)).toEqual({ enOnly: [], zhOnly: [] });
  });

  it('tnSources does not collide with the main catalog', () => {
    // Parse the raw catalog text: the imported JSON object is the live i18n
    // resource store, which registerFeatureLocales has already merged into.
    const enUS = JSON.parse(enUSRaw);
    expect(() => assertNoNamespaceCollision('tnSources', enUS as Record<string, unknown>)).not.toThrow();
    expect(Object.keys(en)).toEqual(['tnSources']);
    expect(Object.keys(zh)).toEqual(['tnSources']);
  });
});

describe('Sources rows', () => {
  it('shows each Source health as a glyph and a word', async () => {
    apiMocks.listAutonomySources.mockResolvedValue({ sources: SOURCES });
    renderWithQuery(<SourcesPanel />);

    await screen.findByText('owner/healthy');
    const expectHealth = (name: string, health: string, word: string, glyph: string) => {
      const mark = rowOf(name).querySelector(`[data-health="${health}"]`) as HTMLElement;
      expect(mark, `${name} health`).not.toBeNull();
      expect(mark).toHaveTextContent(word);
      expect(mark.querySelector('[aria-hidden="true"]')).toHaveTextContent(glyph);
    };
    expectHealth('owner/healthy', 'ok', 'healthy', '✔');
    expectHealth('owner/broken', 'error', 'error', '✖');
    expectHealth('owner/paused', 'paused', 'paused', '⊘');
    expectHealth('owner/fresh', 'waiting', 'not polled', '○');
    expect(within(rowOf('owner/broken')).getByText('HTTP 401 bad credentials')).toBeInTheDocument();
  });

  it('fetches job counts per Source and renders them as labelled tabular cells', async () => {
    apiMocks.listAutonomySources.mockResolvedValue({ sources: [SOURCES[0]] });
    renderWithQuery(<SourcesPanel />);

    await screen.findByText('owner/healthy');
    const row = rowOf('owner/healthy');
    const counts = within(row).getByRole('group', { name: 'Job counts' });
    await waitFor(() => expect(within(counts).getByLabelText('1 running')).toBeInTheDocument());
    expect(within(counts).getByLabelText('1 queued')).toBeInTheDocument();
    expect(within(counts).getByLabelText('1 needs you')).toBeInTheDocument();
    expect(within(counts).getByLabelText('1 failed')).toBeInTheDocument();
    expect(within(counts).getByLabelText('5 jobs')).toBeInTheDocument();
    expect(apiMocks.listAutonomyTickets).toHaveBeenCalledWith({ source_id: 'source-ok' });
    expect(counts).toHaveClass('tn-sources-counts');
  });

  it('marks a total that hit the page cap as a lower bound', async () => {
    apiMocks.listAutonomySources.mockResolvedValue({ sources: [SOURCES[0]] });
    apiMocks.listAutonomyTickets.mockResolvedValue({
      tickets: Array.from({ length: 200 }, () => ({ status: 'completed', attention: null })),
    });
    renderWithQuery(<SourcesPanel />);
    const total = await screen.findByLabelText('200 jobs');
    expect(total).toHaveTextContent('200+');
  });

  it('keeps every row action a keyboard-reachable button inside a labelled group', async () => {
    const user = userEvent.setup();
    apiMocks.listAutonomySources.mockResolvedValue({ sources: [SOURCES[0]] });
    renderWithQuery(<SourcesPanel />);
    await screen.findByText('owner/healthy');

    const group = screen.getByRole('group', { name: 'Actions for owner/healthy' });
    const names = within(group).getAllByRole('button').map((b) => b.textContent);
    expect(names).toEqual(['Edit', 'Tickets', 'Check', 'Poll now', 'Disable', 'Delete']);

    const checkButton = within(group).getByRole('button', { name: 'Check' });
    for (let i = 0; i < 20 && document.activeElement !== checkButton; i += 1) await user.tab();
    expect(checkButton).toHaveFocus();
    await user.keyboard('{Enter}');
    await waitFor(() => expect(apiMocks.checkAutonomySource).toHaveBeenCalledWith('source-ok'));
    expect(await screen.findByText('checked as conductor-bot · read-only')).toBeInTheDocument();

    await user.click(within(group).getByRole('button', { name: 'Poll now' }));
    await waitFor(() => expect(apiMocks.pollAutonomySource).toHaveBeenCalledWith('source-ok'));
    await user.click(within(group).getByRole('button', { name: 'Disable' }));
    await waitFor(() => expect(apiMocks.updateAutonomySource).toHaveBeenCalledWith('source-ok', { enabled: false }));
  });

  it('deletes only after the confirm dialog agrees', async () => {
    const user = userEvent.setup();
    apiMocks.listAutonomySources.mockResolvedValue({ sources: [SOURCES[0]] });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
    renderWithQuery(<SourcesPanel />);
    const group = await screen.findByRole('group', { name: 'Actions for owner/healthy' });

    await user.click(within(group).getByRole('button', { name: 'Delete' }));
    expect(confirm).toHaveBeenLastCalledWith('Delete the Source for owner/healthy?');
    expect(apiMocks.deleteAutonomySource).not.toHaveBeenCalled();
    await user.click(within(group).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(apiMocks.deleteAutonomySource).toHaveBeenCalledWith('source-ok'));
  });
});

describe('Source form (behaviour unchanged)', () => {
  it.each([
    ['github', 'Repository', 'owner/repo'],
    ['feishu', 'Feishu chat id', 'oc_chat'],
  ] as const)('creates a %s Source with its selected daemon, then checks it', async (kind, field, value) => {
    const user = userEvent.setup();
    renderWithQuery(<SourcesPanel />);

    await user.selectOptions(screen.getByLabelText('Source kind'), kind);
    await user.type(screen.getByLabelText(field), value);
    await user.selectOptions(screen.getByLabelText('Workspace'), 'ws-1');
    await user.selectOptions(await screen.findByLabelText('Run Tickets on'), 'daemon-1');
    expect(screen.getByText('The selected daemon provides the Worker runtime.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Add Source' }));

    await waitFor(() => expect(apiMocks.createAutonomySource).toHaveBeenCalledWith(expect.objectContaining({
      kind,
      enabled: true,
      config: expect.objectContaining({ workspace_id: 'ws-1', worker_daemon_id: 'daemon-1' }),
    })));
    await waitFor(() => expect(apiMocks.checkAutonomySource).toHaveBeenCalledWith('source-created'));
  });

  it('Edit loads the Source and Save sends null when the daemon is cleared', async () => {
    const user = userEvent.setup();
    apiMocks.listAutonomySources.mockResolvedValue({ sources: [SOURCES[0]] });
    renderWithQuery(<SourcesPanel />);

    await user.click(await screen.findByRole('button', { name: 'Edit' }));
    expect(screen.getByText('Edit GitHub Source')).toBeInTheDocument();
    expect(screen.getByLabelText('Repository')).toHaveValue('owner/healthy');
    expect(screen.getByLabelText('Run Tickets on')).toHaveValue('daemon-1');
    expect(screen.getByLabelText('Worker agent')).toHaveValue('codex');
    await user.selectOptions(screen.getByLabelText('Run Tickets on'), '');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(apiMocks.updateAutonomySource).toHaveBeenCalledWith('source-ok', {
      config: expect.objectContaining({ repository: 'owner/healthy', worker_daemon_id: null }),
    }));
  });
});

describe('Scheduled task rows', () => {
  const schedule = {
    schedule_id: 'schedule-1', title: 'Daily', prompt: 'Review', workspace_id: 'ws-1',
    cron_expression: '0 9 * * 1-5', timezone: 'UTC', worker_provider: 'codex', worker_daemon_id: null,
    enabled: false, next_run_at: '2026-11-03T09:00:00Z', last_run_at: null, last_error: '',
  };

  it('shows paused health, schedule job counts and keeps Duplicate prefilling a copy', async () => {
    const user = userEvent.setup();
    apiMocks.listAutonomySchedules.mockResolvedValue({ schedules: [schedule] });
    ticketsFor['schedule-1'] = [{ status: 'running', attention: null }, { status: 'completed', attention: null }];
    renderWithQuery(<SchedulePanel />);

    await screen.findByText('Daily');
    const row = rowOf('Daily');
    expect(row.querySelector('[data-health="paused"]')).toHaveTextContent('paused');
    await waitFor(() => expect(within(row).getByLabelText('2 jobs')).toBeInTheDocument());
    expect(apiMocks.listAutonomyTickets).toHaveBeenCalledWith({ schedule_id: 'schedule-1' });

    await user.click(within(row).getByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(apiMocks.updateAutonomySchedule).toHaveBeenCalledWith('schedule-1', { enabled: true }));
    await user.click(within(row).getByRole('button', { name: 'Duplicate' }));
    expect(screen.getByLabelText('Title')).toHaveValue('Daily (copy)');
    delete ticketsFor['schedule-1'];
  });
});

describe('SourcesPage in zh-CN', () => {
  it('renders every section, health word and action from tnSources', async () => {
    await i18n.changeLanguage('zh-CN');
    apiMocks.listAutonomySources.mockResolvedValue({ sources: [SOURCES[0]] });
    renderWithQuery(<SourcesPage />);

    expect(await screen.findByRole('heading', { level: 1, name: '来源' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: '定时任务' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: '主动观察' })).toBeInTheDocument();
    await screen.findByText('owner/healthy');
    const row = rowOf('owner/healthy');
    expect(row.querySelector('[data-health="ok"]')).toHaveTextContent('正常');
    const group = within(row).getByRole('group', { name: 'owner/healthy 的操作' });
    expect(within(group).getByRole('button', { name: '立即轮询' })).toBeInTheDocument();
    await waitFor(() => expect(within(row).getByLabelText('1 运行中')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: '添加来源' })).toBeInTheDocument();
    // No raw keys leak into the page.
    expect(document.body.textContent).not.toMatch(/tnSources\./);
  });
});
