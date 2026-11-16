import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';

const apiMocks = vi.hoisted(() => ({
  listAutonomySources: vi.fn(),
  listAutonomyDaemons: vi.fn(),
  listAutonomyTickets: vi.fn(),
  listAutonomySchedules: vi.fn(),
  getAutonomyTicket: vi.fn(),
  getAutonomyTicketRaw: vi.fn(),
  getTicketProgress: vi.fn(),
  stopAutonomyTicket: vi.fn(),
  retryAutonomyTicket: vi.fn(),
  discardAutonomyTicket: vi.fn(),
  replyAutonomyTicket: vi.fn(),
  getTaskActivity: vi.fn(),
  closeAutonomyTicket: vi.fn(),
  getTicketClarification: vi.fn(),
  confirmTicketClarification: vi.fn(),
  createAutonomySource: vi.fn(),
  updateAutonomySource: vi.fn(),
  checkAutonomySource: vi.fn(),
  pollAutonomySource: vi.fn(),
  deleteAutonomySource: vi.fn(),
  createManualTask: vi.fn(),
}));

vi.mock('@/api/autonomy', () => apiMocks);
vi.mock('@/hooks/useAllWorkspaces', () => ({
  useAllWorkspaces: () => ({ data: { workspaces: [{ workspace_id: 'ws-1', name: 'Project' }] } }),
}));
vi.mock('@/hooks/useNativeAgents', () => ({ useNativeAgents: () => ({ data: [] }) }));

import { TicketDetailPage } from '../TaskPage';

const ticket = {
  ticket_id: 'ticket-1',
  user_id: 'user-1',
  workspace_id: 'ws-1',
  title: 'Speed up the dashboard',
  scope: 'Make the dashboard load faster.',
  status: 'awaiting_clarification',
  mandate: {},
  origin_trigger_id: null,
  origin_thread_key: 'manual:abc123',
  router_thread_id: null,
  worker_thread_id: null,
  worker_run_id: null,
  attempts: 0,
  attention: null,
  summary: '',
  error: '',
  created_at: '2026-11-02T00:00:00Z',
  updated_at: '2026-11-02T00:00:00Z',
  inputs: [],
  reports: [],
};

const clarification = {
  clarification_id: 'clar-1',
  ticket_id: 'ticket-1',
  status: 'pending',
  clarity_score: 0.32,
  original_prompt: 'make it faster',
  expanded_request: 'Optimize the query path so the dashboard loads in under 2 seconds.',
  assumptions: ['PostgreSQL 16 is available', 'No schema changes are allowed'],
  open_questions: ['Is 2s the p95 or the mean target?'],
  confirmed_text: null,
  created_at: '2026-11-02T00:00:00Z',
};

function renderWithQuery(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const renderPage = () => renderWithQuery(<TicketDetailPage ticketId="ticket-1" />);
const trapInput = () => screen.findByTestId('ticket-clarify-input');
const composerInput = () => screen.getByTestId('ticket-composer-input');

beforeEach(() => {
  vi.clearAllMocks();
  apiMocks.getAutonomyTicket.mockResolvedValue(ticket);
  apiMocks.getTaskActivity.mockResolvedValue({ ticket_id: 'ticket-1', events: [] });
  apiMocks.getTicketProgress.mockResolvedValue(null);
  apiMocks.closeAutonomyTicket.mockResolvedValue(ticket);
  apiMocks.replyAutonomyTicket.mockResolvedValue({});
  apiMocks.listAutonomySources.mockResolvedValue({ sources: [] });
  apiMocks.listAutonomySchedules.mockResolvedValue({ schedules: [] });
  apiMocks.getTicketClarification.mockResolvedValue(clarification);
  apiMocks.confirmTicketClarification.mockResolvedValue({ ...ticket, status: 'queued' });
});

describe('clarification gate on the ticket detail page', () => {
  it('shows the draft for an awaiting_clarification ticket and confirms the edited text', async () => {
    const user = userEvent.setup();
    let resolveConfirm: (value: unknown) => void = () => undefined;
    apiMocks.confirmTicketClarification.mockImplementation(
      () => new Promise((resolve) => { resolveConfirm = resolve; }),
    );
    renderPage();

    const textarea = await trapInput();
    expect(textarea).toHaveValue(clarification.expanded_request);
    expect(screen.getByText('Confirm the request before this ticket starts')).toBeInTheDocument();
    expect(screen.getByText(clarification.original_prompt)).toBeInTheDocument();
    expect(screen.getByText('PostgreSQL 16 is available')).toBeInTheDocument();
    expect(screen.getByText('No schema changes are allowed')).toBeInTheDocument();
    expect(screen.getByText('Is 2s the p95 or the mean target?')).toBeInTheDocument();
    expect(screen.getByText(/Clarity score 0\.32 — below the 0\.6 threshold/)).toBeInTheDocument();
    // The trap wears the needs-you state glyph and word.
    const trap = textarea.closest('[data-trap="clarify"]') as HTMLElement;
    expect(trap.querySelector('.sa-state--needs-you .sa-state-glyph')).toHaveTextContent('◆');

    await user.clear(textarea);
    await user.type(textarea, 'Edited request text');
    const button = screen.getByRole('button', { name: 'Confirm & start' });
    await user.click(button);

    expect(button).toBeDisabled();
    await waitFor(() =>
      expect(apiMocks.confirmTicketClarification).toHaveBeenCalledWith('ticket-1', 'Edited request text'),
    );
    resolveConfirm({ ...ticket, status: 'queued' });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm & start' })).not.toBeDisabled());
  });

  it('confirms the drafted text when "Confirm as drafted" is clicked after an edit', async () => {
    const user = userEvent.setup();
    renderPage();

    const textarea = await trapInput();
    await user.clear(textarea);
    await user.type(textarea, 'Something else entirely');
    await user.click(screen.getByRole('button', { name: 'Confirm as drafted' }));

    await waitFor(() =>
      expect(apiMocks.confirmTicketClarification).toHaveBeenCalledWith('ticket-1', clarification.expanded_request),
    );
  });

  it('does not render the panel for tickets in any other status', async () => {
    apiMocks.getAutonomyTicket.mockResolvedValue({ ...ticket, status: 'queued' });
    renderPage();

    await screen.findByRole('heading', { name: ticket.title });
    expect(screen.queryByText('Confirm the request before this ticket starts')).not.toBeInTheDocument();
    expect(screen.queryByTestId('ticket-clarify-input')).not.toBeInTheDocument();
    expect(apiMocks.getTicketClarification).not.toHaveBeenCalled();
  });

  it('mounts the panel from the live page with the same { ticketId } API', async () => {
    renderWithQuery(<TicketDetailPage ticketId="ticket-1" />);
    expect(await trapInput()).toHaveValue(clarification.expanded_request);
    expect(apiMocks.getTicketClarification).toHaveBeenCalledWith('ticket-1');
  });
});

describe('the clarification trap sits in front of the composer', () => {
  it('comes before the composer in document and Tab order', async () => {
    const user = userEvent.setup();
    renderPage();
    const trap = await trapInput();
    const composer = composerInput();
    expect(trap.compareDocumentPosition(composer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    trap.focus();
    // trap → Confirm & start → Confirm as drafted → composer
    await user.tab();
    expect(screen.getByRole('button', { name: 'Confirm & start' })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: 'Confirm as drafted' })).toHaveFocus();
    await user.tab();
    expect(composer).toHaveFocus();
  });

  it('Escape in the trap hands focus to the composer and keeps the draft', async () => {
    const user = userEvent.setup();
    renderPage();
    const trap = await trapInput();
    await user.click(trap);
    await user.type(trap, ' (p95)');
    await user.keyboard('{Escape}');

    expect(composerInput()).toHaveFocus();
    expect(trap).toHaveValue(`${clarification.expanded_request} (p95)`);
    expect(apiMocks.confirmTicketClarification).not.toHaveBeenCalled();
  });

  it('the composer does not send an instruction while the trap is pending', async () => {
    const user = userEvent.setup();
    renderPage();
    await trapInput();
    const composer = composerInput();
    expect(composer).toHaveAttribute('readonly');
    await user.click(composer);
    await user.keyboard('{Enter}');

    expect(apiMocks.replyAutonomyTicket).not.toHaveBeenCalled();
    expect(apiMocks.confirmTicketClarification).not.toHaveBeenCalled();
    expect(screen.getByText(/Confirm the request above first/, { selector: '[role="status"]' })).toBeInTheDocument();
  });

  it('Enter in the trap confirms the edited request, once', async () => {
    const user = userEvent.setup();
    let resolveConfirm: (value: unknown) => void = () => undefined;
    apiMocks.confirmTicketClarification.mockImplementation(
      () => new Promise((resolve) => { resolveConfirm = resolve; }),
    );
    renderPage();
    const trap = await trapInput();
    await user.click(trap);
    await user.type(trap, ' p95');
    // Two Enters before React re-renders must still confirm once.
    fireEvent.keyDown(trap, { key: 'Enter' });
    fireEvent.keyDown(trap, { key: 'Enter' });

    await waitFor(() => expect(apiMocks.confirmTicketClarification).toHaveBeenCalledTimes(1));
    expect(apiMocks.confirmTicketClarification).toHaveBeenCalledWith(
      'ticket-1',
      `${clarification.expanded_request} p95`,
    );
    resolveConfirm({ ...ticket, status: 'queued' });
    expect(await screen.findByText('confirmed · the Ticket is queued')).toBeInTheDocument();
  });
});

describe('newline keys and the IME guard', () => {
  it('Shift+Enter and Ctrl+J insert a newline at the caret in the trap and never confirm', async () => {
    const user = userEvent.setup();
    renderPage();
    const trap = (await trapInput()) as HTMLTextAreaElement;
    await user.clear(trap);
    await user.type(trap, 'ab');
    trap.setSelectionRange(1, 1);
    await user.keyboard('{Shift>}{Enter}{/Shift}');
    expect(trap).toHaveValue('a\nb');
    expect(trap.selectionStart).toBe(2);
    await user.keyboard('{Control>}j{/Control}');
    expect(trap).toHaveValue('a\n\nb');
    expect(apiMocks.confirmTicketClarification).not.toHaveBeenCalled();
  });

  it('Shift+Enter and Ctrl+J insert a newline in the composer; Enter sends the instruction', async () => {
    const user = userEvent.setup();
    apiMocks.getAutonomyTicket.mockResolvedValue({ ...ticket, status: 'running' });
    renderPage();
    await screen.findByRole('heading', { name: ticket.title });
    const composer = composerInput();
    expect(composer).not.toHaveAttribute('readonly');
    await user.click(composer);
    await user.type(composer, 'one');
    await user.keyboard('{Shift>}{Enter}{/Shift}two{Control>}j{/Control}three');
    expect(composer).toHaveValue('one\ntwo\nthree');
    expect(apiMocks.replyAutonomyTicket).not.toHaveBeenCalled();

    await user.keyboard('{Enter}');
    await waitFor(() => expect(apiMocks.replyAutonomyTicket).toHaveBeenCalledWith('ticket-1', 'one\ntwo\nthree'));
    await waitFor(() => expect(composer).toHaveValue(''));
  });

  it.each([
    ['isComposing', { key: 'Enter', isComposing: true }],
    ['keyCode 229', { key: 'Enter', keyCode: 229 }],
    ['Shift+Enter while composing', { key: 'Enter', shiftKey: true, isComposing: true }],
    ['Ctrl+J while composing', { key: 'j', ctrlKey: true, keyCode: 229 }],
  ])('a composing Enter (%s) never confirms, sends or inserts', async (_label, init) => {
    renderPage();
    const trap = await trapInput();
    const before = (trap as HTMLTextAreaElement).value;
    const notCancelled = fireEvent.keyDown(trap, init);
    expect(notCancelled).toBe(true);
    expect(trap).toHaveValue(before);
    expect(apiMocks.confirmTicketClarification).not.toHaveBeenCalled();
  });

  it('a composing Enter in the composer never sends or inserts', async () => {
    apiMocks.getAutonomyTicket.mockResolvedValue({ ...ticket, status: 'running' });
    renderPage();
    await screen.findByRole('heading', { name: ticket.title });
    const composer = composerInput();
    fireEvent.change(composer, { target: { value: '你好' } });
    expect(fireEvent.keyDown(composer, { key: 'Enter', isComposing: true })).toBe(true);
    expect(fireEvent.keyDown(composer, { key: 'Enter', keyCode: 229 })).toBe(true);
    expect(composer).toHaveValue('你好');
    expect(apiMocks.replyAutonomyTicket).not.toHaveBeenCalled();

    fireEvent.keyDown(composer, { key: 'Enter' });
    await waitFor(() => expect(apiMocks.replyAutonomyTicket).toHaveBeenCalledWith('ticket-1', '你好'));
  });
});
