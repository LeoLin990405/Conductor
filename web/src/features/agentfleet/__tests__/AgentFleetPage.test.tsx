import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import i18n from 'i18next';

const apiMocks = vi.hoisted(() => ({
  getAgentFleet: vi.fn(),
  getAgentLogs: vi.fn(),
}));

vi.mock('@/api/agentFleet', () => apiMocks);

import AgentFleetPage from '../AgentFleetPage';

const FLEET = {
  agents: [
    {
      id: 'agent-fleet',
      model: 'codex',
      provider: 'codex',
      role: 'dev' as const,
      pid: 101,
      worktree: '/Users/you/conductor-wt/agent-fleet',
      branch: 'feat/agent-fleet',
      status: 'running' as const,
      elapsed_secs: 3723,
      last_activity_ts: '2026-11-02T12:00:00Z',
      log_file: 'agent-fleet.log',
      log_tail: 'starting work\nAuthorization: [REDACTED]',
      commits_ahead: 3,
      tests_state: '12 passed',
      provider_balance: 'ok' as const,
    },
    {
      id: 'memfw-r2',
      model: 'mimo',
      provider: 'mimo',
      role: 'review' as const,
      pid: null,
      worktree: '/Users/you/conductor-wt/memfw-r2',
      branch: 'feat/memfw-r2',
      status: 'stalled' as const,
      elapsed_secs: null,
      last_activity_ts: '2026-11-02T09:00:00Z',
      log_file: '',
      log_tail: '',
      commits_ahead: null,
      tests_state: null,
      provider_balance: 'low' as const,
    },
  ],
  scan_ts: '2026-11-02T12:00:00Z',
  stall_threshold_mins: 5,
  error: null,
};

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AgentFleetPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMocks.getAgentFleet.mockResolvedValue(FLEET);
  apiMocks.getAgentLogs.mockResolvedValue({
    agent_id: 'agent-fleet',
    log_file: 'agent-fleet.log',
    lines: ['starting work', 'Authorization: [REDACTED]'],
    truncated: false,
  });
});

describe('AgentFleetPage', () => {
  it('renders one row per agent with its dashboard columns', async () => {
    renderPage();

    expect(await screen.findByText('agent-fleet')).toBeInTheDocument();
    expect(screen.getByText('memfw-r2')).toBeInTheDocument();
    expect(screen.getByText('feat/agent-fleet')).toBeInTheDocument();
    expect(screen.getByText('1:02:03')).toBeInTheDocument();
    expect(screen.getByText('12 passed')).toBeInTheDocument();
    // Unknown cells render as a dash rather than a zero.
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
    // Both balance states are labelled, never a raw amount.
    const table = screen.getByRole('table');
    expect(within(table).getByTitle('Balance ok')).toBeInTheDocument();
    expect(within(table).getByTitle('Balance low')).toBeInTheDocument();
  });

  it('labels every status in words', async () => {
    renderPage();
    await screen.findByText('agent-fleet');
    const row = screen.getByRole('row', { name: /agent-fleet/ });
    expect(within(row).getByText('Running')).toBeInTheDocument();
    const stalledRow = screen.getByRole('row', { name: /memfw-r2/ });
    expect(within(stalledRow).getByText('Stalled')).toBeInTheDocument();
  });

  it('filters the table by status', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('agent-fleet');

    await user.selectOptions(screen.getByLabelText('Status'), 'stalled');

    await waitFor(() => expect(screen.queryByText('agent-fleet')).not.toBeInTheDocument());
    expect(screen.getByText('memfw-r2')).toBeInTheDocument();
  });

  it('filters the table by provider substring', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('agent-fleet');

    await user.type(screen.getByLabelText('Provider'), 'mimo');

    await waitFor(() => expect(screen.queryByText('agent-fleet')).not.toBeInTheDocument());
    expect(screen.getByText('memfw-r2')).toBeInTheDocument();
  });

  it('opens the redacted log viewer for one agent', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('agent-fleet');

    await user.click(screen.getByRole('button', { name: 'agent-fleet' }));

    await waitFor(() => expect(apiMocks.getAgentLogs).toHaveBeenCalledWith('agent-fleet', 200));
    const panel = screen.getByRole('heading', { name: 'agent-fleet' }).closest('section');
    expect(panel).not.toBeNull();
    expect(within(panel as HTMLElement).getByText(/starting work/)).toBeInTheDocument();
    expect(within(panel as HTMLElement).getByText(/Authorization: \[REDACTED\]/)).toBeInTheDocument();
  });

  it('explains a loopback refusal instead of showing an empty table', async () => {
    apiMocks.getAgentFleet.mockRejectedValue({ response: { status: 403 } });
    renderPage();

    expect(await screen.findByText(/only available from that machine/)).toBeInTheDocument();
    expect(screen.queryByText('agent-fleet')).not.toBeInTheDocument();
  });

  it('surfaces a disabled-fleet note from the server', async () => {
    apiMocks.getAgentFleet.mockResolvedValue({
      agents: [],
      scan_ts: '2026-11-02T12:00:00Z',
      stall_threshold_mins: 5,
      error: 'Agent fleet discovery is disabled on this deployment.',
    });
    renderPage();

    expect(await screen.findByText(/disabled on this deployment/)).toBeInTheDocument();
    // A disabled deployment is a scan that never ran, not one that found
    // nothing, so neither the table nor the empty note is drawn.
    expect(screen.queryByText('No agents match the current filters.')).not.toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('explains a failed scan the backend answered with a 200', async () => {
    apiMocks.getAgentFleet.mockResolvedValue({
      agents: [],
      scan_ts: '2026-11-02T12:00:00Z',
      stall_threshold_mins: 5,
      error: 'The fleet scan failed; the worktrees were not read.',
    });
    renderPage();

    expect(await screen.findByText(/worktrees were not read/)).toBeInTheDocument();
    expect(await screen.findByText(/fleet scan could not be read/)).toBeInTheDocument();
    expect(screen.queryByText('No agents match the current filters.')).not.toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it.each([
    ['a server failure', 500],
    ['a dropped connection', undefined],
  ])('explains %s instead of drawing an empty fleet', async (_label, status) => {
    apiMocks.getAgentFleet.mockRejectedValue(
      status === undefined ? new Error('network down') : { response: { status } },
    );
    renderPage();

    expect(await screen.findByText(/fleet scan could not be read/)).toBeInTheDocument();
    // An unanswered scan is not a scan that found nothing.
    expect(screen.queryByText('No agents match the current filters.')).not.toBeInTheDocument();
    expect(screen.queryByText('agent-fleet')).not.toBeInTheDocument();
  });

  it('draws every status and balance as a glyph and a word, with no lucide icon', async () => {
    const { container } = renderPage();
    await screen.findByText('agent-fleet');
    const running = screen.getByRole('row', { name: /agent-fleet/ });
    const runningMark = within(running).getByText('Running').closest('.tn-fleet-mark');
    expect(runningMark?.querySelector('.tn-fleet-glyph')?.textContent).toBe('●');
    expect(runningMark?.className).toContain('tn-fleet-mark--tempo');
    const stalled = screen.getByRole('row', { name: /memfw-r2/ });
    const stalledMark = within(stalled).getByText('Stalled').closest('.tn-fleet-mark');
    expect(stalledMark?.querySelector('.tn-fleet-glyph')?.textContent).toBe('▣');
    const low = within(stalled).getByTitle('Balance low');
    expect(low.querySelector('.tn-fleet-glyph')?.textContent).toBe('◆');
    expect(within(low).getByText('low')).toBeInTheDocument();
    // The old page drew lucide svgs and a 999px pill; the glyph grammar has neither.
    expect(container.querySelector('svg')).toBeNull();
    expect(container.querySelector('.af-status')).toBeNull();
  });

  it('labels every stacked cell so a 390px row keeps its column names', async () => {
    renderPage();
    await screen.findByText('agent-fleet');
    const row = screen.getByRole('row', { name: /agent-fleet/ });
    const labels = within(row)
      .getAllByRole('cell')
      .map((cell) => cell.getAttribute('data-label'));
    expect(labels).toEqual([
      'Agent',
      'Model',
      'Role',
      'Worktree',
      'Status',
      'Elapsed',
      'Log tail',
      'Ahead',
      'Tests',
      'Balance',
    ]);
  });

  it('lists the provider roster from the data, one dense row per provider', async () => {
    const providers = ['codex', 'kimi', 'cursor', 'grok', 'minimax', 'deepseek-dsh', 'zcode', 'stepfun'];
    apiMocks.getAgentFleet.mockResolvedValue({
      ...FLEET,
      agents: providers.map((provider, index) => ({
        ...FLEET.agents[0],
        id: `agent-${index}`,
        provider,
        model: `${provider}-model`,
        status: index % 2 ? ('done' as const) : ('running' as const),
        provider_balance: index === 3 ? ('low' as const) : ('ok' as const),
      })),
    });
    renderPage();
    await screen.findByText('agent-0');

    const roster = screen.getByRole('region', { name: 'Providers' });
    const rows = within(roster).getAllByRole('listitem');
    // Name order, exactly the providers the scan reported.
    expect(rows.map((row) => row.getAttribute('data-provider'))).toEqual([...providers].sort());
    const grok = rows.find((row) => row.getAttribute('data-provider') === 'grok') as HTMLElement;
    expect(within(grok).getByText('grok-model')).toBeInTheDocument();
    expect(within(grok).getByTitle('Balance low')).toBeInTheDocument();
    const codex = rows.find((row) => row.getAttribute('data-provider') === 'codex') as HTMLElement;
    expect(within(codex).getByTitle('Balance ok')).toBeInTheDocument();
  });

  it('a roster provider button filters the agents and toggles back from the keyboard', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('agent-fleet');
    const roster = screen.getByRole('region', { name: 'Providers' });
    const mimo = within(roster).getByRole('button', { name: 'mimo' });
    expect(mimo).toHaveAttribute('aria-pressed', 'false');

    mimo.focus();
    await user.keyboard('{Enter}');

    await waitFor(() => expect(screen.queryByText('agent-fleet')).not.toBeInTheDocument());
    expect(screen.getByLabelText('Provider')).toHaveValue('mimo');
    expect(mimo).toHaveAttribute('aria-pressed', 'true');
    // The roster still names every provider while one is picked.
    expect(within(roster).getByRole('button', { name: 'codex' })).toBeInTheDocument();

    await user.keyboard(' ');
    expect(await screen.findByText('agent-fleet')).toBeInTheDocument();
    expect(screen.getByLabelText('Provider')).toHaveValue('');
  });

  it('no filter placeholder names a provider or model', async () => {
    renderPage();
    await screen.findByText('agent-fleet');
    for (const name of ['Provider', 'Model']) {
      const placeholder = screen.getByLabelText(name).getAttribute('placeholder') ?? '';
      expect(placeholder).not.toMatch(/codex|glm|kimi/i);
    }
  });

  it('renders the page in zh-CN from the tnAgentFleet catalog', async () => {
    await i18n.changeLanguage('zh-CN');
    try {
      renderPage();
      expect(await screen.findByRole('heading', { level: 1, name: 'Agent 集群' })).toBeInTheDocument();
      await screen.findByText('agent-fleet');
      const row = screen.getByRole('row', { name: /agent-fleet/ });
      expect(within(row).getByText('运行中')).toBeInTheDocument();
      expect(within(row).getByText('开发')).toBeInTheDocument();
      expect(screen.getByRole('region', { name: '供应商' })).toBeInTheDocument();
      expect(screen.getByLabelText('状态')).toBeInTheDocument();
    } finally {
      await i18n.changeLanguage('en-US');
    }
  });
});
