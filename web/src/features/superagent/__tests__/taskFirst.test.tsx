import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';

const apiMocks = vi.hoisted(() => ({
  getAutonomyTicket: vi.fn(),
  getAutonomyTicketRaw: vi.fn(),
  getTaskActivity: vi.fn(),
  getAutonomyOverview: vi.fn(),
  listAutonomySources: vi.fn(),
  listAutonomySchedules: vi.fn(),
  listAutonomyTickets: vi.fn(),
  listAutonomyDaemons: vi.fn(),
  createManualTask: vi.fn(),
  closeAutonomyTicket: vi.fn(),
  retryAutonomyTicket: vi.fn(),
  discardAutonomyTicket: vi.fn(),
  getTicketProgress: vi.fn(),
  replyAutonomyTicket: vi.fn(),
  getTicketClarification: vi.fn(),
  confirmTicketClarification: vi.fn(),
}));

const paletteMocks = vi.hoisted(() => ({ openPalette: vi.fn() }));

vi.mock('@/api/autonomy', () => apiMocks);
vi.mock('@/features/palette/PaletteHost', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/features/palette/PaletteHost')>()),
  openPalette: paletteMocks.openPalette,
}));
vi.mock('@/hooks/useAllWorkspaces', () => ({ useAllWorkspaces: vi.fn() }));
vi.mock('@/hooks/useNativeAgents', () => ({ useNativeAgents: vi.fn() }));

import { TicketDetailPage } from '../TaskPage';
import InboxPage from '../InboxPage';
import { __resetComposerModeForTests, cycleComposerMode } from '@/features/palette/composerMode';
import i18n from 'i18next';
import { useAllWorkspaces } from '@/hooks/useAllWorkspaces';
import { useNativeAgents } from '@/hooks/useNativeAgents';

const ticket = {
  ticket_id: 't-1',
  user_id: 'u-1',
  workspace_id: 'ws-1',
  title: 'Fix the save button',
  scope: 'Change the label',
  status: 'queued',
  mandate: {},
  source_id: '',
  trigger_id: '',
  origin_trigger_id: null,
  origin_thread_key: 'manual:task',
  router_thread_id: null,
  worker_thread_id: null,
  worker_run_id: null,
  worker_provider: '',
  worker_model: '',
  attempts: 0,
  attention: null,
  summary: '',
  error: '',
  created_at: '2026-11-03T12:00:00Z',
  updated_at: '2026-11-03T12:05:00Z',
  started_at: null,
  ended_at: null,
  closed_at: null,
  close_reason: '',
  inputs: [],
  reports: [],
};

const activity = {
  ticket_id: 't-1',
  events: [
    {
      event_id: 'created:t-1',
      kind: 'created',
      at: '2026-11-03T12:00:00Z',
      summary: 'Fix the save button',
      detail: 'Change the label',
      state: '',
    },
    {
      event_id: 'input:i1',
      kind: 'instruction',
      at: '2026-11-03T12:02:00Z',
      summary: 'Use the dev branch',
      detail: '',
      state: '',
    },
  ],
};

function renderDetail() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TicketDetailPage ticketId="t-1" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  __resetComposerModeForTests();
  localStorage.removeItem('sa.workspace');
  apiMocks.getAutonomyTicket.mockResolvedValue(ticket);
  apiMocks.getTaskActivity.mockResolvedValue(activity);
  apiMocks.getAutonomyTicketRaw.mockResolvedValue({ events: [], note: null });
  apiMocks.getTicketProgress.mockResolvedValue(null);
  apiMocks.listAutonomySources.mockResolvedValue({ sources: [] });
  apiMocks.listAutonomySchedules.mockResolvedValue({ schedules: [] });
  apiMocks.listAutonomyTickets.mockResolvedValue({ tickets: [] });
  apiMocks.listAutonomyDaemons.mockResolvedValue({ daemons: [] });
  apiMocks.getAutonomyOverview.mockResolvedValue({
    counts: { attention: 1, running: 0, queued: 0, completed: 0, failed: 0 },
    sources: 0,
    enabled_sources: 0,
  });
  apiMocks.createManualTask.mockResolvedValue({ ...ticket, ticket_id: 't-new' });
  apiMocks.closeAutonomyTicket.mockResolvedValue({ ...ticket, status: 'cancelled', closed_at: '2026-11-03T12:06:00Z' });
  apiMocks.replyAutonomyTicket.mockResolvedValue({ input_id: 'i2', status: 'queued_on_task' });
  apiMocks.getTicketClarification.mockRejectedValue(new Error('Request failed with status code 404'));
  apiMocks.confirmTicketClarification.mockResolvedValue({ ...ticket, status: 'queued' });
  vi.mocked(useAllWorkspaces).mockReturnValue({ data: { workspaces: [] } } as never);
  vi.mocked(useNativeAgents).mockReturnValue({ data: [] } as never);
});

// Live markup copy (tnTicket.* in locales/tnTicket.en-US.json).
const COMPOSER_PLACEHOLDER = 'Reply to the worker, or press / at column 0…';
const SEND = 'send';
const DAEMON_NOTE = 'This daemon task takes an instruction only while it is blocked or waiting.';
const CLOSED_NOTE = 'This task takes no more instructions.';
const LOG_NOTE = 'This log is the worker session. The task above is what you track and close.';
const ticketComposer = () => screen.findByTestId('ticket-composer-input');

describe('task-first ticket detail', () => {
  it('tracks the task and adds an instruction without a worker session', async () => {
    const user = userEvent.setup();
    const { container } = renderDetail();

    expect(await screen.findByRole('heading', { name: 'Fix the save button' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Conversation' })).toBeInTheDocument();
    expect(await screen.findByText('Use the dev branch')).toBeInTheDocument();
    expect(container.querySelector('[data-kind="created"] .tn-ticket-who')).toHaveTextContent(/^Created/);
    expect(screen.queryByRole('link', { name: /Open run/i })).not.toBeInTheDocument();
    expect(screen.queryByText(LOG_NOTE)).not.toBeInTheDocument();

    await user.type(screen.getByPlaceholderText(COMPOSER_PLACEHOLDER), 'Check the tests');
    await user.click(screen.getByRole('button', { name: SEND }));

    await waitFor(() =>
      expect(apiMocks.replyAutonomyTicket).toHaveBeenCalledWith('t-1', 'Check the tests'),
    );
    expect(apiMocks.getAutonomyTicketRaw).not.toHaveBeenCalled();
  });

  it('shows an instruction error when a reply is rejected', async () => {
    apiMocks.replyAutonomyTicket.mockRejectedValue(new Error('Request failed with status code 409'));
    const user = userEvent.setup();
    renderDetail();
    await user.type(
      await screen.findByPlaceholderText(COMPOSER_PLACEHOLDER),
      'Check the tests',
    );
    await user.click(screen.getByRole('button', { name: SEND }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not add this instruction. Retry in a moment.',
    );
  });

  it('shows a close error when the worker does not stop', async () => {
    apiMocks.closeAutonomyTicket.mockRejectedValue(new Error('Could not stop execution'));
    const user = userEvent.setup();
    renderDetail();
    await user.click(await screen.findByRole('button', { name: 'Close task' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not close this task. The worker is still running.',
    );
  });

  it('closes the task object', async () => {
    const user = userEvent.setup();
    renderDetail();
    await user.click(await screen.findByRole('button', { name: 'Close task' }));
    await waitFor(() => expect(apiMocks.closeAutonomyTicket).toHaveBeenCalledWith('t-1'));
  });

  it.each(['queued', 'running', 'failed'])(
    'does not accept an instruction on a %s daemon task',
    async (status) => {
      apiMocks.getAutonomyTicket.mockResolvedValue({
        ...ticket,
        status,
        worker_daemon_id: 'daemon-1',
      });
      renderDetail();
      const composer = await ticketComposer();
      expect(composer).toHaveAttribute('readonly');
      expect(composer).toHaveAttribute('placeholder', DAEMON_NOTE);
      expect(screen.queryByPlaceholderText(COMPOSER_PLACEHOLDER)).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: SEND })).toBeDisabled();
      expect(screen.getByText(DAEMON_NOTE, { selector: '[role="status"]' })).toBeInTheDocument();
      fireEvent.keyDown(composer, { key: 'Enter' });
      expect(await mutationFired(apiMocks.replyAutonomyTicket)).toBe(false);
    },
  );

  it('shows a daemon rejection instead of failing silently', async () => {
    apiMocks.getAutonomyTicket.mockResolvedValue({
      ...ticket,
      status: 'blocked',
      worker_daemon_id: 'daemon-1',
    });
    apiMocks.replyAutonomyTicket.mockRejectedValue(new Error('Request failed with status code 409'));
    const user = userEvent.setup();
    renderDetail();

    await user.type(
      await screen.findByPlaceholderText(COMPOSER_PLACEHOLDER),
      'Use the dev branch',
    );
    await user.click(screen.getByRole('button', { name: SEND }));

    expect(
      await screen.findByRole('alert'),
    ).toHaveTextContent(
      'Could not add this instruction. A daemon task accepts one only while it is blocked or waiting.',
    );
    expect(apiMocks.replyAutonomyTicket).toHaveBeenCalledWith('t-1', 'Use the dev branch');
  });

  it('still offers an instruction while a daemon task is waiting', async () => {
    apiMocks.getAutonomyTicket.mockResolvedValue({
      ...ticket,
      status: 'waiting',
      worker_daemon_id: 'daemon-1',
    });
    const user = userEvent.setup();
    renderDetail();
    const composer = await ticketComposer();
    expect(composer).not.toHaveAttribute('readonly');
    expect(composer).toHaveAttribute('placeholder', COMPOSER_PLACEHOLDER);
    await user.type(composer, 'Check the tests');
    expect(screen.getByRole('button', { name: SEND })).toBeEnabled();
    expect(screen.queryByText(/takes an instruction only while/)).not.toBeInTheDocument();
  });

  it('hides instructions after the task is closed and keeps the session log collapsed', async () => {
    apiMocks.getAutonomyTicket.mockResolvedValue({
      ...ticket,
      status: 'completed',
      closed_at: '2026-11-03T12:06:00Z',
      close_reason: 'Signed off',
    });
    renderDetail();
    expect(await screen.findByText(/Closed · Signed off/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Close task' })).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText(COMPOSER_PLACEHOLDER)).not.toBeInTheDocument();
    const composer = await ticketComposer();
    expect(composer).toHaveAttribute('readonly');
    expect(screen.getByRole('button', { name: SEND })).toBeDisabled();
    expect(screen.getByText(CLOSED_NOTE, { selector: '[role="status"]' })).toBeInTheDocument();
    expect(screen.queryByText(/worker session/)).not.toBeInTheDocument();
  });
});

const needsYouTicket = {
  ...ticket,
  ticket_id: 't-needs',
  title: 'Pin the palette boundary with a test',
  status: 'blocked',
  attention: { question: 'Should I pin the boundary with a unit test on both surfaces first?' },
  worker_provider: 'kimi',
  worker_model: 'kimi-k3.1-pro-coding',
  updated_at: '2026-11-03T12:05:00Z',
};

function renderInbox() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <InboxPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/**
 * Whether a mutation fired within a short window. The send a composer Enter
 * triggers is an async react-query mutation, so a synchronous
 * `not.toHaveBeenCalled()` right after the key would pass even when the guard
 * that should have blocked it is gone. The IME tests assert this stays false:
 * delete either guard and the Enter fires the mutation, flipping this true.
 */
async function mutationFired(mock: { mock: { calls: unknown[] } }): Promise<boolean> {
  return waitFor(
    () => {
      if (mock.mock.calls.length > 0) return true;
      throw new Error('mutation has not fired yet');
    },
    { timeout: 250 },
  ).then(
    () => true,
    () => false,
  );
}

describe('terminal-native inbox composer', () => {
  it('shows the needs-you inbox first, with the reply trap before the composer', async () => {
    apiMocks.listAutonomyTickets.mockResolvedValue({ tickets: [needsYouTicket] });
    renderInbox();

    const reply = await screen.findByTestId('inbox-reply-input');
    const composer = screen.getByTestId('inbox-composer-input');
    // The reply trap is a trap in front of the composer: it precedes it in the DOM.
    expect(reply.compareDocumentPosition(composer) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    // The needs-you section is rendered before the running/composer sections.
    expect(screen.getByRole('heading', { name: /Needs you/ })).toBeInTheDocument();
  });

  it('reply trap: a bare Enter sends the reply and inserts no newline', async () => {
    apiMocks.listAutonomyTickets.mockResolvedValue({ tickets: [needsYouTicket] });
    const user = userEvent.setup();
    renderInbox();

    const reply = await screen.findByTestId('inbox-reply-input');
    await user.type(reply, 'yes pin it first{Enter}');

    await waitFor(() =>
      expect(apiMocks.replyAutonomyTicket).toHaveBeenCalledWith('t-needs', 'yes pin it first'),
    );
    // Sent (and cleared), never turned into a multi-line draft.
    expect(reply).toHaveValue('');
  });

  it('reply trap: Shift+Enter and Ctrl+J insert a newline instead of sending', async () => {
    apiMocks.listAutonomyTickets.mockResolvedValue({ tickets: [needsYouTicket] });
    const user = userEvent.setup();
    renderInbox();

    const reply = await screen.findByTestId('inbox-reply-input');
    await user.type(reply, 'first line');
    await user.keyboard('{Shift>}{Enter}{/Shift}');
    await user.type(reply, 'second line');
    await user.keyboard('{Control>}j{/Control}');
    await user.type(reply, 'third line');

    expect(reply).toHaveValue('first line\nsecond line\nthird line');
    expect(apiMocks.replyAutonomyTicket).not.toHaveBeenCalled();
  });

  it('reply trap: Enter while an IME reports isComposing (only) does nothing', async () => {
    apiMocks.listAutonomyTickets.mockResolvedValue({ tickets: [needsYouTicket] });
    const user = userEvent.setup();
    renderInbox();

    const reply = await screen.findByTestId('inbox-reply-input');
    await user.type(reply, 'draft');
    // Only the isComposing flag is set (keyCode is 0). The Enter is inert solely
    // because of the isComposing guard: delete that guard and the reply mutation
    // fires, which mutationFired catches.
    fireEvent.keyDown(reply, { key: 'Enter', isComposing: true });

    expect(await mutationFired(apiMocks.replyAutonomyTicket), 'composing Enter must not send').toBe(false);
    expect(reply).toHaveValue('draft');
  });

  it('reply trap: Enter carrying keyCode 229 (only) does nothing', async () => {
    apiMocks.listAutonomyTickets.mockResolvedValue({ tickets: [needsYouTicket] });
    const user = userEvent.setup();
    renderInbox();

    const reply = await screen.findByTestId('inbox-reply-input');
    await user.type(reply, 'draft');
    // Only the 229 keyCode is set (isComposing is false). The Enter is inert
    // solely because of the keyCode guard: delete that guard and the reply
    // mutation fires, which mutationFired catches.
    fireEvent.keyDown(reply, { key: 'Enter', keyCode: 229, isComposing: false });

    expect(await mutationFired(apiMocks.replyAutonomyTicket), 'keyCode 229 Enter must not send').toBe(false);
    expect(reply).toHaveValue('draft');
  });

  it('composer: a bare Enter sends, and never inserts a newline', async () => {
    localStorage.setItem('sa.workspace', 'ws-1');
    vi.mocked(useAllWorkspaces).mockReturnValue({
      data: { workspaces: [{ workspace_id: 'ws-1', name: 'WS', is_home: false }] },
    } as never);
    apiMocks.listAutonomyTickets.mockResolvedValue({ tickets: [needsYouTicket] });
    const user = userEvent.setup();
    renderInbox();

    const composer = await screen.findByTestId('inbox-composer-input');
    await user.type(composer, 'build the thing{Enter}');

    await waitFor(() => expect(apiMocks.createManualTask).toHaveBeenCalled());
    expect(apiMocks.createManualTask.mock.calls[0][0]).toMatchObject({ prompt: 'build the thing' });
  });

  it('composer: Shift+Enter inserts a newline and does not send', async () => {
    localStorage.setItem('sa.workspace', 'ws-1');
    vi.mocked(useAllWorkspaces).mockReturnValue({
      data: { workspaces: [{ workspace_id: 'ws-1', name: 'WS', is_home: false }] },
    } as never);
    const user = userEvent.setup();
    renderInbox();

    const composer = await screen.findByTestId('inbox-composer-input');
    await user.type(composer, 'line one');
    await user.keyboard('{Shift>}{Enter}{/Shift}');
    await user.type(composer, 'line two');

    expect(composer).toHaveValue('line one\nline two');
    expect(apiMocks.createManualTask).not.toHaveBeenCalled();
  });

  it('composer: Enter while an IME reports isComposing (only) does nothing', async () => {
    // A valid workspace is configured, so the composer can submit at all: if the
    // isComposing guard were removed this Enter would create a task and fail.
    localStorage.setItem('sa.workspace', 'ws-1');
    vi.mocked(useAllWorkspaces).mockReturnValue({
      data: { workspaces: [{ workspace_id: 'ws-1', name: 'WS', is_home: false }] },
    } as never);
    apiMocks.listAutonomyTickets.mockResolvedValue({ tickets: [needsYouTicket] });
    const user = userEvent.setup();
    renderInbox();

    const composer = await screen.findByTestId('inbox-composer-input');
    await user.type(composer, 'draft');
    // Only the isComposing flag is set (keyCode is 0).
    fireEvent.keyDown(composer, { key: 'Enter', isComposing: true });

    expect(await mutationFired(apiMocks.createManualTask), 'composing Enter must not create').toBe(false);
    expect(composer).toHaveValue('draft');
  });

  it('composer: Enter carrying keyCode 229 (only) does nothing', async () => {
    // A valid workspace is configured, so the composer can submit at all: if the
    // keyCode guard were removed this Enter would create a task and fail.
    localStorage.setItem('sa.workspace', 'ws-1');
    vi.mocked(useAllWorkspaces).mockReturnValue({
      data: { workspaces: [{ workspace_id: 'ws-1', name: 'WS', is_home: false }] },
    } as never);
    apiMocks.listAutonomyTickets.mockResolvedValue({ tickets: [needsYouTicket] });
    const user = userEvent.setup();
    renderInbox();

    const composer = await screen.findByTestId('inbox-composer-input');
    await user.type(composer, 'draft');
    // Only the 229 keyCode is set (isComposing is false).
    fireEvent.keyDown(composer, { key: 'Enter', keyCode: 229, isComposing: false });

    expect(await mutationFired(apiMocks.createManualTask), 'keyCode 229 Enter must not create').toBe(false);
    expect(composer).toHaveValue('draft');
  });

  it('composer: a second Enter while creation is pending creates only one task', async () => {
    localStorage.setItem('sa.workspace', 'ws-1');
    vi.mocked(useAllWorkspaces).mockReturnValue({
      data: { workspaces: [{ workspace_id: 'ws-1', name: 'WS', is_home: false }] },
    } as never);
    apiMocks.listAutonomyTickets.mockResolvedValue({ tickets: [needsYouTicket] });
    // A create that never resolves keeps the mutation in flight across both Enters.
    apiMocks.createManualTask.mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup();
    renderInbox();

    const composer = await screen.findByTestId('inbox-composer-input');
    await user.type(composer, 'build');
    // Two Enters inside one React batch: `create.isPending` is stale on the
    // second, so only the synchronous in-flight guard keeps this at one call.
    act(() => {
      fireEvent.keyDown(composer, { key: 'Enter' });
      fireEvent.keyDown(composer, { key: 'Enter' });
    });
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(apiMocks.createManualTask).toHaveBeenCalledTimes(1);
  });

  it('reply trap: Escape returns focus to the composer and preserves the draft', async () => {
    apiMocks.listAutonomyTickets.mockResolvedValue({ tickets: [needsYouTicket] });
    renderInbox();

    const reply = await screen.findByTestId('inbox-reply-input');
    const composer = screen.getByTestId('inbox-composer-input');
    reply.focus();
    fireEvent.change(reply, { target: { value: 'a half-written reply' } });
    fireEvent.keyDown(reply, { key: 'Escape' });

    expect(composer).toHaveFocus();
    // The trap hands focus back without discarding what was typed.
    expect(reply).toHaveValue('a half-written reply');
  });

  it('does not claim nothing needs you when the overview counts a question the page never surfaced', async () => {
    // 200 newer completed Tickets crowd the pending question off the capped
    // page; the overview's authoritative attention count must still suppress the
    // empty message.
    const completed = Array.from(
      { length: 200 },
      (_, i) => ({ ...ticket, ticket_id: `done-${i}`, status: 'completed' }),
    );
    apiMocks.listAutonomyTickets.mockResolvedValue({ tickets: completed });
    apiMocks.getAutonomyOverview.mockResolvedValue({
      counts: { attention: 1, running: 0, queued: 0, completed: 200, failed: 0 },
      sources: 0,
      enabled_sources: 0,
    });
    renderInbox();

    expect(await screen.findByRole('heading', { name: /Needs you/ })).toBeInTheDocument();
    expect(screen.queryByText(/Nothing needs you/)).not.toBeInTheDocument();
  });
});

type TicketFilters = { status?: string; failed?: boolean; attention?: boolean };

/**
 * A filter-aware ticket list: each Inbox category query gets only the rows the
 * backend would return for its filter, so a category the page forgets to query
 * really is missing (the blanket mock hands every query every row).
 */
function routeTickets(byFilter: {
  attention?: unknown[];
  failed?: unknown[];
  status?: Record<string, unknown[]>;
}) {
  apiMocks.listAutonomyTickets.mockImplementation(async (filters: TicketFilters = {}) => {
    if (filters.attention) return { tickets: byFilter.attention ?? [] };
    if (filters.failed) return { tickets: byFilter.failed ?? [] };
    if (filters.status) return { tickets: byFilter.status?.[filters.status] ?? [] };
    return { tickets: [] };
  });
}

function overviewCounts(counts: Partial<{ attention: number; running: number; queued: number; failed: number; blocked: number }>) {
  apiMocks.getAutonomyOverview.mockResolvedValue({
    counts: { attention: 0, running: 0, queued: 0, completed: 0, failed: 0, blocked: 0, ...counts },
    sources: 0,
    enabled_sources: 0,
  });
}

const rows = (n: number, prefix: string, extra: Record<string, unknown>) =>
  Array.from({ length: n }, (_, i) => ({ ...ticket, ticket_id: `${prefix}-${i}`, ...extra }));

const clarification = {
  clarification_id: 'clar-1',
  ticket_id: 't-clar',
  status: 'pending',
  clarity_score: 0.32,
  original_prompt: 'make it faster',
  expanded_request: 'Optimize the query path so the dashboard loads in under 2 seconds.',
  assumptions: [],
  open_questions: ['Is 2s the p95 or the mean target?'],
  confirmed_text: null,
  created_at: '2026-11-03T12:00:00Z',
};

describe('inbox needs-you categories and totals', () => {
  it('shows a waiting Ticket with no question in Needs you', async () => {
    const waiting = { ...ticket, ticket_id: 't-wait', title: 'Review the pending diff', status: 'waiting', attention: null };
    routeTickets({ status: { waiting: [waiting] } });
    overviewCounts({});
    renderInbox();

    expect(await screen.findByRole('button', { name: 'Review the pending diff' })).toBeInTheDocument();
    expect(screen.queryByText(/Nothing needs you/)).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /Needs you/ })).toHaveTextContent('◆ 1');
  });

  it('shows an awaiting_clarification Ticket with no question in Needs you', async () => {
    const parked = { ...ticket, ticket_id: 't-clar', title: 'Speed up the dashboard', status: 'awaiting_clarification', attention: null };
    routeTickets({ status: { awaiting_clarification: [parked] } });
    apiMocks.getTicketClarification.mockResolvedValue(clarification);
    overviewCounts({});
    renderInbox();

    expect(await screen.findByRole('button', { name: 'Speed up the dashboard' })).toBeInTheDocument();
    expect(screen.queryByText(/Nothing needs you/)).not.toBeInTheDocument();
  });

  it('counts a capped attention page plus a disjoint failed Ticket exactly (201 + 1 = 202)', async () => {
    // The attention list is cut at the 200-row cap while the overview knows of
    // 201; one failed Ticket without a question is in no attention row.
    routeTickets({
      attention: rows(200, 'ask', { status: 'running', attention: { question: 'Which branch?' } }),
      failed: [{ ...ticket, ticket_id: 'boom', status: 'failed', attention: null }],
    });
    overviewCounts({ attention: 201, failed: 1 });
    renderInbox();

    const heading = await screen.findByRole('heading', { name: /Needs you/ });
    await waitFor(() => expect(heading).toHaveTextContent('◆ 202'));
    expect(heading).not.toHaveTextContent('+');
    expect(screen.getByText(/202 needs you/)).toBeInTheDocument();
  });

  it('does not double-count a failed Ticket that also carries a question', async () => {
    const both = { ...ticket, ticket_id: 'both', status: 'failed', attention: { question: 'Retry?' } };
    routeTickets({ attention: [both], failed: [both] });
    overviewCounts({ attention: 1, failed: 1 });
    renderInbox();

    const heading = await screen.findByRole('heading', { name: /Needs you/ });
    await waitFor(() => expect(heading).toHaveTextContent('◆ 1'));
    expect(heading).not.toHaveTextContent('◆ 2');
  });

  it('marks the needs-you total as partial when a question-free category is capped', async () => {
    routeTickets({ failed: rows(200, 'boom', { status: 'failed', attention: null }) });
    overviewCounts({ failed: 250 });
    renderInbox();

    const heading = await screen.findByRole('heading', { name: /Needs you/ });
    await waitFor(() => expect(heading).toHaveTextContent('◆ 200+'));
  });

  it('reads the running and queued totals from the overview, not the capped page', async () => {
    routeTickets({
      status: {
        running: rows(200, 'run', { status: 'running' }),
        queued: rows(200, 'q', { status: 'queued' }),
      },
    });
    overviewCounts({ running: 201, queued: 230 });
    renderInbox();

    const runningHeading = await screen.findByRole('heading', { name: /Running/ });
    await waitFor(() => expect(runningHeading).toHaveTextContent('● 201'));
    expect(screen.getByRole('heading', { name: /Queued/ })).toHaveTextContent('○ 230');
    expect(screen.getByText(/201 running/)).toBeInTheDocument();
  });

  it('shows a capped running page as a lower bound until the overview lands', async () => {
    routeTickets({ status: { running: rows(200, 'run', { status: 'running' }) } });
    apiMocks.getAutonomyOverview.mockReturnValue(new Promise(() => {}));
    renderInbox();

    const runningHeading = await screen.findByRole('heading', { name: /Running/ });
    expect(runningHeading).toHaveTextContent('● 200+');
  });
});

describe('inbox loading, locale and mode', () => {
  it('does not claim "Nothing needs you" before the waiting and clarification lists load', async () => {
    // Overview and the attention/failed lists answer empty; the waiting and
    // awaiting_clarification lists (not in the overview counts) are in flight.
    apiMocks.listAutonomyTickets.mockImplementation((filters: TicketFilters = {}) =>
      filters.status === 'waiting' || filters.status === 'awaiting_clarification'
        ? new Promise(() => {})
        : Promise.resolve({ tickets: [] }),
    );
    overviewCounts({});
    renderInbox();

    const heading = await screen.findByRole('heading', { name: /Needs you/ });
    await waitFor(() => expect(apiMocks.getAutonomyOverview).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(screen.queryByText(/Nothing needs you/)).not.toBeInTheDocument();
    expect(heading).not.toHaveTextContent('◆ 0');
  });

  it('says "Nothing needs you" once every needs-you source is loaded and empty', async () => {
    routeTickets({});
    overviewCounts({});
    renderInbox();
    expect(await screen.findByText(/Nothing needs you/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /Needs you/ })).toHaveTextContent('◆ 0');
  });

  it('localizes the row age and the composer mode in zh-CN', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date('2026-11-03T21:05:00Z') });
    await i18n.changeLanguage('zh-CN');
    try {
      routeTickets({ attention: [needsYouTicket] });
      overviewCounts({ attention: 1 });
      renderInbox();
      // updated_at is 12:05Z, nine hours before "now".
      expect(await screen.findByText(/等待 9 小时前/)).toBeInTheDocument();
      expect(screen.queryByText(/ago/)).not.toBeInTheDocument();
      expect(screen.getByTestId('inbox-composer-mode')).toHaveTextContent('自动');
      act(() => {
        cycleComposerMode();
        cycleComposerMode();
      });
      expect(screen.getByTestId('inbox-composer-mode')).toHaveTextContent('只读');
    } finally {
      await i18n.changeLanguage('en-US');
      vi.useRealTimers();
    }
  });

  it('the composer mode label follows composerMode as Alt+M cycles it', async () => {
    renderInbox();
    const label = await screen.findByTestId('inbox-composer-mode');
    expect(label).toHaveTextContent('auto');
    // S03's PaletteHost binds Alt+M to cycleComposerMode; drive that same API.
    act(() => void cycleComposerMode());
    expect(label).toHaveTextContent('dispatch');
    act(() => void cycleComposerMode());
    expect(label).toHaveTextContent('read-only');
    act(() => void cycleComposerMode());
    expect(label).toHaveTextContent('auto');
  });
});

describe('inbox clarification trap', () => {
  const parked = {
    ...ticket,
    ticket_id: 't-clar',
    title: 'Speed up the dashboard',
    status: 'awaiting_clarification',
    attention: { question: 'Confirm the request before this ticket starts' },
  };

  it('prefills the drafted request and Enter confirms it through the clarification route', async () => {
    routeTickets({ attention: [parked], status: { awaiting_clarification: [parked] } });
    apiMocks.getTicketClarification.mockResolvedValue(clarification);
    overviewCounts({ attention: 1 });
    const user = userEvent.setup();
    renderInbox();

    const trap = await screen.findByTestId('inbox-reply-input');
    expect(trap).toHaveValue(clarification.expanded_request);
    expect(screen.getByText('Is 2s the p95 or the mean target?')).toBeInTheDocument();
    await user.click(trap);
    await user.keyboard('{End} (p95){Enter}');

    await waitFor(() =>
      expect(apiMocks.confirmTicketClarification).toHaveBeenCalledWith(
        't-clar',
        `${clarification.expanded_request} (p95)`,
      ),
    );
    // Never stored as an instruction that would leave the Ticket parked.
    expect(apiMocks.replyAutonomyTicket).not.toHaveBeenCalled();
    expect(await screen.findByText(/confirmed · the Ticket is queued/)).toBeInTheDocument();
  });

  it('does not re-read the confirmed (now gone) clarification and flash "unavailable"', async () => {
    routeTickets({ attention: [parked], status: { awaiting_clarification: [parked] } });
    apiMocks.getTicketClarification.mockResolvedValueOnce(clarification);
    overviewCounts({ attention: 1 });
    renderInbox();

    const trap = await screen.findByTestId('inbox-reply-input');
    fireEvent.keyDown(trap, { key: 'Enter' });
    await waitFor(() => expect(apiMocks.confirmTicketClarification).toHaveBeenCalled());
    await screen.findByText(/confirmed · the Ticket is queued/);
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(apiMocks.getTicketClarification).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('inbox-clarify-unavailable')).not.toBeInTheDocument();
  });

  it('offers no instruction trap when the drafted request cannot be read', async () => {
    routeTickets({ attention: [parked] });
    overviewCounts({ attention: 1 });
    renderInbox();

    expect(await screen.findByTestId('inbox-clarify-unavailable')).toBeInTheDocument();
    expect(screen.queryByTestId('inbox-reply-input')).not.toBeInTheDocument();
    expect(apiMocks.replyAutonomyTicket).not.toHaveBeenCalled();
  });
});

describe('inbox keyboard guards', () => {
  it('reply trap: a second Enter while the reply is pending sends only once', async () => {
    apiMocks.listAutonomyTickets.mockResolvedValue({ tickets: [needsYouTicket] });
    apiMocks.replyAutonomyTicket.mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup();
    renderInbox();

    const reply = await screen.findByTestId('inbox-reply-input');
    await user.type(reply, 'yes');
    // Both Enters land before React re-renders, so `isPending` is still stale
    // on the second: only a synchronous in-flight guard stops the duplicate.
    act(() => {
      fireEvent.keyDown(reply, { key: 'Enter' });
      fireEvent.keyDown(reply, { key: 'Enter' });
    });

    await waitFor(() => expect(apiMocks.replyAutonomyTicket).toHaveBeenCalled());
    // Give a duplicate send time to land before asserting there was none.
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(apiMocks.replyAutonomyTicket).toHaveBeenCalledTimes(1);
  });

  it('composer: "/" at column 0 opens the palette prefilled with "/" and inserts nothing', async () => {
    const user = userEvent.setup();
    renderInbox();

    const composer = await screen.findByTestId('inbox-composer-input');
    await user.click(composer);
    await user.keyboard('/');

    expect(paletteMocks.openPalette).toHaveBeenCalledTimes(1);
    expect(paletteMocks.openPalette).toHaveBeenCalledWith(composer, '/');
    expect(composer).toHaveValue('');
  });

  it('composer: "/" after text is ordinary text and opens nothing', async () => {
    const user = userEvent.setup();
    renderInbox();

    const composer = await screen.findByTestId('inbox-composer-input');
    await user.type(composer, 'a/b');

    expect(paletteMocks.openPalette).not.toHaveBeenCalled();
    expect(composer).toHaveValue('a/b');
  });
});

describe('newline chords are handled by the composer, not the browser default', () => {
  // fireEvent performs no default action, so the newline appears only if the
  // composer's own key contract inserts it (and keeps the caret after it).
  it.each([
    ['Shift+Enter', { key: 'Enter', shiftKey: true }],
    ['Ctrl+J', { key: 'j', ctrlKey: true }],
  ])('reply trap: %s inserts a newline at the caret without sending', async (_name, chord) => {
    apiMocks.listAutonomyTickets.mockResolvedValue({ tickets: [needsYouTicket] });
    renderInbox();
    const reply = (await screen.findByTestId('inbox-reply-input')) as HTMLTextAreaElement;
    fireEvent.change(reply, { target: { value: 'ab' } });
    reply.focus();
    reply.setSelectionRange(1, 1);
    fireEvent.keyDown(reply, chord);

    expect(reply).toHaveValue('a\nb');
    expect(reply.selectionStart).toBe(2);
    expect(await mutationFired(apiMocks.replyAutonomyTicket)).toBe(false);
  });

  it.each([
    ['Shift+Enter', { key: 'Enter', shiftKey: true }],
    ['Ctrl+J', { key: 'j', ctrlKey: true }],
  ])('composer: %s inserts a newline without creating a task', async (_name, chord) => {
    localStorage.setItem('sa.workspace', 'ws-1');
    vi.mocked(useAllWorkspaces).mockReturnValue({
      data: { workspaces: [{ workspace_id: 'ws-1', name: 'WS', is_home: false }] },
    } as never);
    renderInbox();
    const composer = (await screen.findByTestId('inbox-composer-input')) as HTMLTextAreaElement;
    fireEvent.change(composer, { target: { value: 'line one' } });
    composer.focus();
    composer.setSelectionRange(8, 8);
    fireEvent.keyDown(composer, chord);

    expect(composer).toHaveValue('line one\n');
    expect(await mutationFired(apiMocks.createManualTask)).toBe(false);
  });
});

describe('composer "/" caret boundaries', () => {
  async function composerWith(value: string, caret: number, caretEnd = caret) {
    renderInbox();
    const composer = (await screen.findByTestId('inbox-composer-input')) as HTMLTextAreaElement;
    fireEvent.change(composer, { target: { value } });
    composer.focus();
    composer.setSelectionRange(caret, caretEnd);
    return composer;
  }

  it('opens the palette right after a newline', async () => {
    const composer = await composerWith('line\n', 5);
    fireEvent.keyDown(composer, { key: '/' });
    expect(paletteMocks.openPalette).toHaveBeenCalledWith(composer, '/');
  });

  it('stays ordinary text mid-line', async () => {
    const composer = await composerWith('ab', 1);
    fireEvent.keyDown(composer, { key: '/' });
    expect(paletteMocks.openPalette).not.toHaveBeenCalled();
  });

  it('does not open across a selection that starts at column 0', async () => {
    const composer = await composerWith('abc', 0, 3);
    fireEvent.keyDown(composer, { key: '/' });
    expect(paletteMocks.openPalette).not.toHaveBeenCalled();
  });

  it('does not open with a modifier held', async () => {
    const composer = await composerWith('', 0);
    fireEvent.keyDown(composer, { key: '/', ctrlKey: true });
    fireEvent.keyDown(composer, { key: '/', altKey: true });
    fireEvent.keyDown(composer, { key: '/', metaKey: true });
    expect(paletteMocks.openPalette).not.toHaveBeenCalled();
  });
});
