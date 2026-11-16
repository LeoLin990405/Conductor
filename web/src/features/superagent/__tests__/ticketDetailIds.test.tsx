import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';

const navigate = vi.hoisted(() => vi.fn());
const apiMocks = vi.hoisted(() => ({
  getAutonomyTicket: vi.fn(),
  getAutonomyTicketRaw: vi.fn(),
  listAutonomySources: vi.fn(),
  listAutonomySchedules: vi.fn(),
  stopAutonomyTicket: vi.fn(),
  retryAutonomyTicket: vi.fn(),
  discardAutonomyTicket: vi.fn(),
  replyAutonomyTicket: vi.fn(),
  getTaskActivity: vi.fn(),
  closeAutonomyTicket: vi.fn(),
  getTicketProgress: vi.fn(),
  getTicketClarification: vi.fn(),
  confirmTicketClarification: vi.fn(),
}));

vi.mock('react-router', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router')>();
  return { ...actual, useNavigate: () => navigate };
});
vi.mock('@/api/autonomy', () => apiMocks);

import { ticketSourceId, ticketTriggerId } from '../shared';
import { TicketDetailPage } from '../TaskPage';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// vitest runs from web/; the stylesheet is read as text, not through the CSS pipeline.
const ticketCss = readFileSync(resolve(process.cwd(), 'src/features/superagent/ticket.css'), 'utf8');

const ticket = {
  ticket_id: 't-1',
  user_id: 'u-1',
  workspace_id: 'ws-1',
  title: 'Fix the save button',
  scope: 'Change the label',
  status: 'queued',
  mandate: { source_id: 'from-mandate', trigger_id: 'from-mandate-trigger', schedule_id: '' },
  source_id: 'src-1',
  trigger_id: 'trg-1',
  origin_trigger_id: 'origin-trg',
  origin_thread_key: 'github:acme/widgets#12',
  router_thread_id: null,
  worker_thread_id: null,
  worker_run_id: null,
  worker_provider: '',
  worker_model: '',
  attempts: 1,
  attention: null,
  summary: '',
  error: '',
  created_at: '2026-11-02T00:00:00Z',
  updated_at: '2026-11-02T01:00:00Z',
  started_at: null,
  ended_at: null,
  inputs: [],
  reports: [],
};

function renderDetail() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TicketDetailPage ticketId="t-1" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function cell(label: string): HTMLElement {
  const node = screen.getByText(label).closest('div');
  if (!node) throw new Error(`missing cell ${label}`);
  return node;
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMocks.getAutonomyTicket.mockResolvedValue(ticket);
  apiMocks.getAutonomyTicketRaw.mockResolvedValue({ events: [] });
  apiMocks.listAutonomySchedules.mockResolvedValue({ schedules: [] });
  apiMocks.listAutonomySources.mockReturnValue(new Promise(() => {}));
  apiMocks.stopAutonomyTicket.mockResolvedValue({});
  apiMocks.retryAutonomyTicket.mockResolvedValue({});
  apiMocks.discardAutonomyTicket.mockResolvedValue({});
  apiMocks.replyAutonomyTicket.mockResolvedValue({});
  apiMocks.getTaskActivity.mockResolvedValue({ ticket_id: 't-1', events: [] });
  apiMocks.closeAutonomyTicket.mockResolvedValue({});
  apiMocks.getTicketProgress.mockResolvedValue(null);
});

describe('ticket source and trigger ids', () => {
  it('prefers explicit ids and falls back to the mandate and origin trigger', () => {
    expect(ticketSourceId({ source_id: 'src-1', mandate: { source_id: 's1' } })).toBe('src-1');
    expect(ticketSourceId({ source_id: '  ', mandate: { source_id: 's1' } })).toBe('s1');
    expect(ticketSourceId({ mandate: { source_id: 's1' } })).toBe('s1');
    expect(ticketTriggerId({
      trigger_id: 'trg-1',
      origin_trigger_id: 'origin-trg',
      mandate: { trigger_id: 'mandate-trigger' },
    })).toBe('trg-1');
    expect(ticketTriggerId({
      origin_trigger_id: 'trg_1',
      mandate: { trigger_id: 'mandate-trigger' },
    })).toBe('trg_1');
    expect(ticketTriggerId({
      origin_trigger_id: null,
      mandate: { trigger_id: 'mandate-trigger' },
    })).toBe('mandate-trigger');
    expect(ticketSourceId({ mandate: {} })).toBe('');
    expect(ticketTriggerId({ origin_trigger_id: null, mandate: {} })).toBe('');
  });

  it('shows both ids before the sources list has loaded', async () => {
    renderDetail();
    expect(await screen.findByRole('heading', { name: 'Fix the save button' })).toBeInTheDocument();
    expect(cell('Source ID').querySelector('code')).toHaveTextContent('src-1');
    expect(cell('Trigger ID').querySelector('code')).toHaveTextContent('trg-1');
    expect(cell('Source')).toHaveTextContent('src-1');
    expect(cell('Source').textContent).not.toContain('—');
    expect(screen.queryByText('from-mandate')).not.toBeInTheDocument();
    expect(screen.queryByText('origin-trg')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Discard plan' })).not.toBeInTheDocument();
  });

  it('shows an accepted observe outcome and discards it without approving', async () => {
    const user = userEvent.setup();
    apiMocks.getAutonomyTicket.mockResolvedValue({
      ...ticket,
      status: 'waiting',
      mandate: { ...ticket.mandate, speculative: true },
      speculative_outcome: {
        outcome: 'accepted',
        evidence: {
          worker_state: 'waiting',
          worker_summary: 'PLAN: check the save button',
          report_id: 'r-1',
        },
      },
      attention: { question: 'Speculative plan ready. Review it, then Retry to execute the work.' },
    });
    renderDetail();
    expect(await screen.findByText('PLAN: check the save button')).toBeInTheDocument();
    expect(cell('Observe outcome')).toHaveTextContent('accepted');
    await user.click(screen.getByRole('button', { name: 'Discard plan' }));
    await waitFor(() => expect(apiMocks.discardAutonomyTicket).toHaveBeenCalledWith('t-1'));
    expect(apiMocks.retryAutonomyTicket).not.toHaveBeenCalled();
  });

  it('shows mandate and origin ids when the payload has no top-level ids', async () => {
    apiMocks.getAutonomyTicket.mockResolvedValue({
      ...ticket,
      source_id: '',
      trigger_id: '',
      origin_trigger_id: 'trg_1',
      mandate: { source_id: 's1' },
    });
    renderDetail();
    expect(await screen.findByRole('heading', { name: 'Fix the save button' })).toBeInTheDocument();
    expect(cell('Source ID').querySelector('code')).toHaveTextContent('s1');
    expect(cell('Trigger ID').querySelector('code')).toHaveTextContent('trg_1');
    expect(cell('Source')).toHaveTextContent('s1');
  });

  it('keeps the repository link and still shows the source id', async () => {
    const user = userEvent.setup();
    apiMocks.listAutonomySources.mockResolvedValue({
      sources: [{
        source_id: 'src-1',
        kind: 'github',
        config: { repository: 'acme/widgets' },
      }],
    });
    renderDetail();
    expect(await screen.findByRole('heading', { name: 'Fix the save button' })).toBeInTheDocument();
    await user.click(await screen.findByRole('button', { name: 'acme/widgets' }));
    expect(navigate).toHaveBeenCalledWith('/tasks?source=src-1');
    expect(cell('Source ID').querySelector('code')).toHaveTextContent('src-1');
    expect(cell('Trigger ID').querySelector('code')).toHaveTextContent('trg-1');
  });

  it('keeps the schedule link and still shows both ids', async () => {
    const user = userEvent.setup();
    apiMocks.getAutonomyTicket.mockResolvedValue({
      ...ticket,
      mandate: { ...ticket.mandate, schedule_id: 'sched-1' },
    });
    apiMocks.listAutonomySchedules.mockResolvedValue({
      schedules: [{ schedule_id: 'sched-1', title: 'Nightly' }],
    });
    renderDetail();
    await user.click(await screen.findByRole('button', { name: 'Nightly' }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/tasks?schedule=sched-1'));
    expect(cell('Source ID').querySelector('code')).toHaveTextContent('src-1');
    expect(cell('Trigger ID').querySelector('code')).toHaveTextContent('trg-1');
  });

  it('prints the full ticket id, branch and base sha in mono cells that wrap anywhere', async () => {
    apiMocks.getAutonomyTicket.mockResolvedValue({
      ...ticket,
      ticket_id: '3419e39e-ca69-4838-844d-547da6333dfc',
      worktree_branch: 'line/review-merge-with-a-very-long-branch-name-that-never-breaks-on-its-own',
      worktree_base_sha: 'b0f4d2189a7c3e1100000000',
      worktree_path: '/srv/conductor/dispatch/ws/review-merge',
    });
    renderDetail();
    const id = await screen.findByTestId('ticket-id');
    expect(id).toHaveTextContent('3419e39e-ca69-4838-844d-547da6333dfc');
    expect(id).toHaveClass('tn-ticket-id');
    const delivery = screen.getByTestId('ticket-delivery');
    for (const key of ['workspace', 'branch', 'base']) {
      expect(delivery.querySelector(`[data-check="${key}"] dd`)).toHaveClass('tn-ticket-code');
    }
    expect(cell('Source ID').querySelector('code')).toHaveClass('tn-ticket-code');
    expect(cell('Trigger ID').querySelector('code')).toHaveClass('tn-ticket-code');
  });

  it('the mono id, origin and code rules break anywhere so nothing overflows at 390', () => {
    const rule = (selector: string) => {
      const match = ticketCss.match(new RegExp(`(^|\\n)${selector.replace('.', '\\.')} \\{([^}]*)\\}`));
      if (!match) throw new Error(`missing ${selector}`);
      return match[2];
    };
    for (const selector of ['.tn-ticket-id', '.tn-ticket-code', '.tn-ticket-origin']) {
      const body = rule(selector);
      expect(body, selector).toMatch(/overflow-wrap:\s*anywhere/);
      expect(body, selector).toMatch(/word-break:\s*break-all/);
      expect(body, selector).toMatch(/min-width:\s*0/);
    }
    // Only token custom properties (with token-hex fallbacks); no radius above radius.card.
    expect(ticketCss).not.toMatch(/border-radius:(?!\s*var\(--radius-(control|card|none))/);
  });
});
