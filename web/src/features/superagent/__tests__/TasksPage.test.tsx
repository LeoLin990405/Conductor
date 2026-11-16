import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import i18n from 'i18next';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { assertNoNamespaceCollision, assertParity } from '@/lib/featureLocales';
import en from '../locales/tnTickets.en-US.json';
import zh from '../locales/tnTickets.zh-CN.json';

const apiMocks = vi.hoisted(() => ({
  listAutonomyTickets: vi.fn(),
  listAutonomySources: vi.fn(),
}));

vi.mock('@/api/autonomy', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/autonomy')>()),
  ...apiMocks,
}));

import TasksPage from '../TasksPage';

const base = {
  user_id: 'u-1',
  workspace_id: 'ws-1',
  scope: '',
  mandate: {},
  origin_trigger_id: null,
  origin_thread_key: 'github:owner/repo#1',
  router_thread_id: null,
  worker_thread_id: null,
  worker_run_id: null,
  worker_provider: 'codex',
  worker_model: 'gpt-5.3-codex',
  worker_daemon_id: null,
  attempts: 1,
  attention: null,
  summary: '',
  error: '',
  created_at: '2026-11-13T10:00:00Z',
  started_at: null,
  ended_at: null,
  closed_at: null,
  close_reason: '',
};

const LONG_ZH = '原生 CLI worker 的上下文轮换不丢失 handoff，并在轮换后继续沿用同一份工作日志与待办清单'.repeat(2);

/** One Ticket per terminal-native state, newest first by updated_at. */
const TICKETS = [
  { ...base, ticket_id: 't-queued', title: 'Queued ticket', status: 'queued', updated_at: '2026-11-13T10:07:00Z' },
  { ...base, ticket_id: 't-running', title: 'Running ticket', status: 'running', worker_provider: 'kimi', updated_at: '2026-11-13T10:06:00Z' },
  { ...base, ticket_id: 't-blocked', title: 'Blocked ticket', status: 'blocked', updated_at: '2026-11-13T10:05:00Z' },
  {
    ...base,
    ticket_id: 't-needs',
    title: LONG_ZH,
    status: 'running',
    attention: { question: 'merge or parallel?' },
    worker_daemon_id: 'studio-mac',
    updated_at: '2026-11-13T10:04:00Z',
  },
  { ...base, ticket_id: 't-failed', title: 'Failed ticket', status: 'failed', updated_at: '2026-11-13T10:03:00Z' },
  { ...base, ticket_id: 't-completed', title: 'Completed ticket', status: 'completed', updated_at: '2026-11-13T10:02:00Z' },
  { ...base, ticket_id: 't-archived', title: 'Archived ticket', status: 'cancelled', updated_at: '2026-11-13T10:01:00Z' },
];

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search}</output>;
}

function renderPage(path = '/tasks') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/tasks" element={<TasksPage />} />
          <Route path="/tasks/:id" element={<p>ticket page</p>} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const rowIds = () => screen.getAllByTestId('ticket-row').map((r) => r.getAttribute('data-ticket-id'));
const selectedId = () =>
  screen.getAllByTestId('ticket-row').find((r) => r.getAttribute('aria-selected') === 'true')?.getAttribute('data-ticket-id');

beforeEach(() => {
  apiMocks.listAutonomyTickets.mockReset();
  apiMocks.listAutonomySources.mockReset();
  apiMocks.listAutonomyTickets.mockImplementation(async (filters: { status?: string } = {}) => ({
    tickets: filters.status ? TICKETS.filter((t) => t.status === filters.status) : TICKETS,
  }));
  apiMocks.listAutonomySources.mockResolvedValue({ sources: [] });
});

afterEach(async () => {
  await i18n.changeLanguage('en-US');
});

describe('TasksPage (terminal-native Tickets list)', () => {
  it('renders every one of the seven states with its glyph and word', async () => {
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId('ticket-row')).toHaveLength(7));
    const expected: Record<string, [string, string, string]> = {
      't-queued': ['queued', '○', 'Queued'],
      't-running': ['running', '●', 'Running'],
      't-blocked': ['blocked', '▣', 'Blocked'],
      't-needs': ['needs-you', '◆', 'Needs you'],
      't-failed': ['failed', '✖', 'Failed'],
      't-completed': ['completed', '✔', 'Completed'],
      't-archived': ['archived', '⊘', 'Archived'],
    };
    for (const row of screen.getAllByTestId('ticket-row')) {
      const [state, glyph, word] = expected[row.getAttribute('data-ticket-id') ?? ''];
      expect(row.getAttribute('data-state')).toBe(state);
      const mark = row.querySelector(`.sa-state--${state}`);
      expect(mark, `${state} mark`).not.toBeNull();
      expect(mark?.querySelector('.sa-state-glyph')?.textContent).toBe(glyph);
      expect(mark?.querySelector('.sa-state-word')?.textContent).toBe(word);
    }
    // The running glyph is the only animated one.
    expect(document.querySelectorAll('tbody .sa-state-glyph--spin')).toHaveLength(1);
  });

  it('renders the seven state words in zh-CN', async () => {
    await i18n.changeLanguage('zh-CN');
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId('ticket-row')).toHaveLength(7));
    const words = screen
      .getAllByTestId('ticket-row')
      .map((r) => r.querySelector('.sa-state-word')?.textContent);
    expect(words).toEqual(['排队中', '运行中', '已阻塞', '需要你', '失败', '已完成', '已归档']);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('工单');
  });

  it('truncates a long title with the full text as its tooltip, in a fixed-layout table', async () => {
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId('ticket-row')).toHaveLength(7));
    const row = screen.getAllByTestId('ticket-row').find((r) => r.getAttribute('data-ticket-id') === 't-needs')!;
    const title = row.querySelector('.tn-tix-title')!;
    expect(title.getAttribute('title')).toBe(LONG_ZH);
    expect(title.textContent).toBe(LONG_ZH);
    // The state cell sits first and the table carries a state column of its own.
    expect(row.firstElementChild?.classList.contains('tn-tix-state')).toBe(true);
    expect(document.querySelector('table.tn-tix col.tn-tix-col-state')).not.toBeNull();
    expect(within(row).getByText('daemon:studio-mac')).toBeInTheDocument();
  });

  it('filters by state with the chips and writes ?state=', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId('ticket-row')).toHaveLength(7));

    await user.click(screen.getByTestId('tickets-state-needs-you'));
    await waitFor(() => expect(rowIds()).toEqual(['t-needs']));
    expect(screen.getByTestId('location')).toHaveTextContent('/tasks?state=needs-you');
    expect(screen.getByTestId('tickets-state-needs-you')).toHaveAttribute('aria-pressed', 'true');

    // A one-status state asks the backend for that status, and keeps the
    // attention Ticket (status running) out of `running`.
    await user.click(screen.getByTestId('tickets-state-running'));
    await waitFor(() => expect(rowIds()).toEqual(['t-running']));
    expect(apiMocks.listAutonomyTickets).toHaveBeenCalledWith(expect.objectContaining({ status: 'running' }));

    await user.click(screen.getByTestId('tickets-state-archived'));
    await waitFor(() => expect(rowIds()).toEqual(['t-archived']));

    await user.click(screen.getByRole('button', { name: 'all' }));
    await waitFor(() => expect(rowIds()).toHaveLength(7));
  });

  it('filters by worker and by text', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId('ticket-row')).toHaveLength(7));
    await user.selectOptions(screen.getByTestId('tickets-worker'), 'kimi');
    await waitFor(() => expect(rowIds()).toEqual(['t-running']));
    expect(screen.getByTestId('location')).toHaveTextContent('worker=kimi');

    await user.selectOptions(screen.getByTestId('tickets-worker'), '');
    await user.type(screen.getByTestId('tickets-search'), 'failed');
    await waitFor(() => expect(rowIds()).toEqual(['t-failed']));
    expect(screen.getByTestId('tickets-summary')).toHaveTextContent('1 of 7');
  });

  it('keeps honouring the legacy ?status= link', async () => {
    renderPage('/tasks?status=blocked');
    await waitFor(() => expect(rowIds()).toEqual(['t-blocked']));
    expect(apiMocks.listAutonomyTickets).toHaveBeenCalledWith(expect.objectContaining({ status: 'blocked' }));
  });

  it('moves with j/k only while the list has focus, and Enter opens the Ticket', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId('ticket-row')).toHaveLength(7));
    expect(selectedId()).toBe('t-queued');

    // j and k typed into the search box are characters, not list motion.
    const search = screen.getByTestId('tickets-search');
    await user.click(search);
    await user.keyboard('j');
    expect(search).toHaveValue('j');
    await user.clear(search);
    await waitFor(() => expect(screen.getAllByTestId('ticket-row')).toHaveLength(7));
    expect(selectedId()).toBe('t-queued');

    // j on the body (list not focused) does nothing either.
    (document.activeElement as HTMLElement | null)?.blur();
    await user.keyboard('j');
    expect(selectedId()).toBe('t-queued');

    // The selected row is the list's single tab stop.
    const rows = screen.getAllByTestId('ticket-row');
    expect(rows.filter((r) => r.tabIndex === 0)).toHaveLength(1);
    rows[0].focus();
    await user.keyboard('j');
    expect(selectedId()).toBe('t-running');
    expect(document.activeElement?.getAttribute('data-ticket-id')).toBe('t-running');
    await user.keyboard('jj');
    expect(selectedId()).toBe('t-needs');
    await user.keyboard('k');
    expect(selectedId()).toBe('t-blocked');
    await user.keyboard('{ArrowDown}');
    expect(selectedId()).toBe('t-needs');

    await user.keyboard('{Enter}');
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/tasks/t-needs'));
  });

  it('"/" outside a text field focuses the search box', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId('ticket-row')).toHaveLength(7));
    screen.getAllByTestId('ticket-row')[0].focus();
    await user.keyboard('/');
    expect(screen.getByTestId('tickets-search')).toHaveFocus();
    expect(screen.getByTestId('tickets-search')).toHaveValue('');
  });

  it('clicking a row opens the Ticket', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId('ticket-row')).toHaveLength(7));
    await user.click(screen.getByText('Failed ticket'));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/tasks/t-failed'));
  });

  it('shows the error copy when the list fails to load', async () => {
    apiMocks.listAutonomyTickets.mockRejectedValue(new Error('boom'));
    renderPage();
    expect(await screen.findByText('Could not read Tickets. Check the connection and retry.')).toBeInTheDocument();
  });
});

describe('tnTickets locales', () => {
  it('keeps en-US / zh-CN key parity', () => {
    expect(assertParity(en, zh)).toEqual({ enOnly: [], zhOnly: [] });
  });

  it('tnTickets does not collide with the main catalog', () => {
    // Read the catalog from disk: the in-memory one already carries the
    // bundle TasksPage registered at module load.
    const enUS = JSON.parse(
      readFileSync(resolve(process.cwd(), 'src/locales/en-US.json'), 'utf8'),
    ) as Record<string, unknown>;
    expect(() => assertNoNamespaceCollision('tnTickets', enUS)).not.toThrow();
    expect(() => assertNoNamespaceCollision('tnInbox', { tnInbox: {} })).toThrow();
    expect(Object.keys(en)).toEqual(['tnTickets']);
    expect(Object.keys(zh)).toEqual(['tnTickets']);
  });
});
