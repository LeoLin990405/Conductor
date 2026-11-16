import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';

const get = vi.hoisted(() => vi.fn());

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof import('@/api/client')>('@/api/client');
  return { ...actual, api: { ...actual.api, get } };
});

import { ApiError } from '@/api/client';
import { getTicketProgress } from '@/api/autonomy';
import TaskPage, { TicketProgressCard } from '../TaskPage';

const LEAK_TRANSCRIPT = 'RAW_WORKER_TRANSCRIPT_do_not_render';
const LEAK_TOOL = 'invoke_tool:read_file_SECRET';

const STEPS = {
  queued: [{ key: 'created', label: '任务已创建', state: 'done' }],
  running: [
    { key: 'created', label: '任务已创建', state: 'done' },
    { key: 'worker', label: '执行中', state: 'active' },
  ],
  waiting: [
    { key: 'created', label: '任务已创建', state: 'done' },
    { key: 'worker', label: '执行中', state: 'done' },
    { key: 'report', label: '结果已生成', state: 'done' },
  ],
  completed: [
    { key: 'created', label: '任务已创建', state: 'done' },
    { key: 'worker', label: '执行中', state: 'done' },
    { key: 'report', label: '结果已生成', state: 'done' },
  ],
  failed: [
    { key: 'created', label: '任务已创建', state: 'done' },
    { key: 'worker', label: '执行中', state: 'failed' },
    { key: 'report', label: '结果已生成', state: 'failed' },
  ],
  cancelled: [
    { key: 'created', label: '任务已创建', state: 'done' },
    { key: 'worker', label: '执行中', state: 'done' },
  ],
} as const;

const PHASE_LABEL: Record<keyof typeof STEPS, string> = {
  queued: 'Queued',
  running: 'Running',
  waiting: 'Waiting',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

/** The terminal-native state each phase wears (tokens.json `status`). */
const PHASE_STATE: Record<keyof typeof STEPS, string> = {
  queued: 'queued',
  running: 'running',
  waiting: 'needs-you',
  completed: 'completed',
  failed: 'failed',
  cancelled: 'archived',
};

const PHASE_GLYPH: Record<keyof typeof STEPS, string> = {
  queued: '○',
  running: '●',
  waiting: '◆',
  completed: '✔',
  failed: '✖',
  cancelled: '⊘',
};

function progress(phase: keyof typeof STEPS) {
  return {
    ticket_id: 't-1',
    status: phase === 'waiting' ? 'blocked' : phase,
    phase,
    elapsed_seconds: 31.2,
    steps: STEPS[phase],
    summary: phase === 'queued' || phase === 'cancelled' ? '' : `summary-${phase}`,
    question: phase === 'waiting' ? '用哪个分支？' : 'HIDDEN_QUESTION_not_waiting',
    error: phase === 'failed' ? 'worker exited 1' : 'HIDDEN_ERROR_not_failed',
    transcript: LEAK_TRANSCRIPT,
    tool_arguments: LEAK_TOOL,
  };
}

function refused(status: number): ApiError {
  return new ApiError(`Request failed with status code ${status}`, status >= 500 ? 'ERR_BAD_RESPONSE' : 'ERR_BAD_REQUEST', {
    data: { detail: 'Ticket not found' },
    status,
    statusText: '',
    headers: {},
  });
}

function renderCard(retry: boolean | number = false) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <TicketProgressCard ticketId="t-1" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  get.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('getTicketProgress', () => {
  it('returns the payload from the progress route', async () => {
    const body = progress('running');
    get.mockResolvedValueOnce({ data: body });
    await expect(getTicketProgress('t-1')).resolves.toEqual(body);
    expect(get).toHaveBeenCalledWith('/api/v1/autonomy/tickets/t-1/progress');
  });

  it('returns null on 404 and throws any other failure', async () => {
    get.mockRejectedValueOnce(refused(404));
    await expect(getTicketProgress('missing')).resolves.toBeNull();

    get.mockRejectedValueOnce(refused(500));
    await expect(getTicketProgress('t-1')).rejects.toThrow(/500/);
  });
});

describe('ticket progress card', () => {
  it.each(['queued', 'running', 'waiting', 'completed', 'failed', 'cancelled'] as const)(
    'renders the %s phase',
    async (phase) => {
      get.mockResolvedValue({ data: progress(phase) });
      renderCard();

      const card = await screen.findByRole('region', { name: 'Worker progress' });
      const word = screen.getByText(PHASE_LABEL[phase]);
      expect(word).toHaveClass('sa-state-word');
      const mark = word.closest('.sa-state');
      expect(mark).toHaveClass(`sa-state--${PHASE_STATE[phase]}`);
      expect(mark?.querySelector('.sa-state-glyph')).toHaveTextContent(PHASE_GLYPH[phase]);
      expect(mark?.querySelector('.sa-state-glyph')).toHaveAttribute('aria-hidden', 'true');
      expect(card).toHaveTextContent('Elapsed');
      expect(card).toHaveTextContent('31.2s');
      expect(card).toHaveTextContent('任务已创建');
      expect(card).not.toHaveTextContent(LEAK_TRANSCRIPT);
      expect(card).not.toHaveTextContent(LEAK_TOOL);
      const summary = `summary-${phase}`;
      if ((phase === 'completed' || phase === 'failed' || phase === 'cancelled') && progress(phase).summary) {
        expect(card.querySelector('.sa-progress-summary')).toHaveTextContent(summary);
      } else {
        expect(card.querySelector('.sa-progress-summary')).not.toBeInTheDocument();
        expect(card).not.toHaveTextContent(summary);
      }
      if (phase === 'waiting') {
        expect(card).toHaveTextContent('用哪个分支？');
        expect(card.querySelector('.sa-progress-question')).toHaveTextContent('用哪个分支？');
      } else {
        expect(card).not.toHaveTextContent('HIDDEN_QUESTION_not_waiting');
        expect(card).not.toHaveTextContent('用哪个分支？');
      }
      if (phase === 'failed') {
        expect(card).toHaveTextContent('worker exited 1');
        expect(card.querySelector('.sa-error')).toHaveTextContent('worker exited 1');
      } else {
        expect(card).not.toHaveTextContent('HIDDEN_ERROR_not_failed');
        expect(card).not.toHaveTextContent('worker exited 1');
      }
    },
  );

  it('emphasizes the question while waiting and the error when failed', async () => {
    get.mockResolvedValue({ data: progress('waiting') });
    const waiting = renderCard();
    expect(await screen.findByText('用哪个分支？')).toHaveClass('sa-progress-question');
    expect(screen.queryByText('HIDDEN_ERROR_not_failed')).not.toBeInTheDocument();
    expect(screen.getByText('执行中').closest('li')).toHaveAttribute('data-state', 'done');
    waiting.unmount();

    get.mockResolvedValue({ data: progress('failed') });
    renderCard();
    const error = await screen.findByText('worker exited 1');
    expect(error).toHaveClass('sa-error');
    expect(screen.queryByText('HIDDEN_QUESTION_not_waiting')).not.toBeInTheDocument();
    expect(screen.getByText('执行中').closest('li')).toHaveAttribute('data-state', 'failed');
    expect(screen.getByText('结果已生成').closest('li')).toHaveAttribute('data-state', 'failed');
  });

  it('marks every step with its state glyph and counts the done steps', async () => {
    get.mockResolvedValue({ data: progress('failed') });
    const failed = renderCard();
    const card = await screen.findByRole('region', { name: 'Worker progress' });
    const marks = [...card.querySelectorAll('li .tn-ticket-step-mark')].map((el) => el.textContent);
    expect(marks).toEqual(['✔', '✖', '✖']);
    expect(card).toHaveTextContent('1 of 3 steps');
    failed.unmount();

    get.mockResolvedValue({ data: progress('running') });
    renderCard();
    const running = await screen.findByRole('region', { name: 'Worker progress' });
    expect([...running.querySelectorAll('li .tn-ticket-step-mark')].map((el) => el.textContent)).toEqual(['✔', '●']);
    expect(running.querySelector('li[aria-current="step"]')).toHaveTextContent('执行中');
    // Only the running phase glyph animates, and only through the spin class.
    expect(running.querySelector('.sa-state-glyph')).toHaveClass('sa-state-glyph--spin');
  });

  it('does not render a worker transcript or tool arguments that arrived beside the card fields', async () => {
    get.mockResolvedValue({ data: { ...progress('running'), body: 'retained final report body' } });
    renderCard();
    const card = await screen.findByRole('region', { name: 'Worker progress' });
    expect(card.querySelector('.sa-progress-summary')).not.toBeInTheDocument();
    expect(card.querySelector('.sa-progress-body')).not.toBeInTheDocument();
    expect(card).not.toHaveTextContent('summary-running');
    expect(card).not.toHaveTextContent('retained final report body');
    expect(card).toHaveTextContent('执行中');
    expect(screen.getByText('执行中').closest('li')).toHaveAttribute('data-state', 'active');
    expect(card.textContent).not.toContain(LEAK_TRANSCRIPT);
    expect(card.textContent).not.toContain(LEAK_TOOL);
    expect(card.textContent).not.toContain('read_file_SECRET');
  });

  it.each(['completed', 'failed', 'cancelled'] as const)(
    'renders the full report body below the summary in %s',
    async (phase) => {
      const body = '## Findings\n| Check | Result |\n| --- | --- |\n| API | Pass |\n\nFinal line.';
      get.mockResolvedValue({ data: { ...progress(phase), body } });
      renderCard();

      const card = await screen.findByRole('region', { name: 'Worker progress' });
      const summary = card.querySelector('.sa-progress-summary');
      const reportBody = card.querySelector('.sa-progress-body');
      if (phase === 'cancelled') {
        expect(summary).not.toBeInTheDocument();
      } else {
        expect(summary).toHaveTextContent(`summary-${phase}`);
        expect(summary?.compareDocumentPosition(reportBody!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
      }
      expect(reportBody?.textContent).toBe(body);
    },
  );

  it('hides a report body identical to the summary after trimming', async () => {
    get.mockResolvedValue({
      data: { ...progress('completed'), summary: 'Same report', body: '  Same report\n' },
    });
    renderCard();

    const card = await screen.findByRole('region', { name: 'Worker progress' });
    expect(card.querySelector('.sa-progress-summary')).toHaveTextContent('Same report');
    expect(card.querySelector('.sa-progress-body')).not.toBeInTheDocument();
  });

  it('shows a report body that differs from the summary', async () => {
    const body = 'Detailed findings beyond the summary.';
    get.mockResolvedValue({ data: { ...progress('completed'), body } });
    renderCard();

    const card = await screen.findByRole('region', { name: 'Worker progress' });
    expect(card.querySelector('.sa-progress-summary')).toHaveTextContent('summary-completed');
    expect(card.querySelector('.sa-progress-body')).toHaveTextContent(body);
  });

  it('renders no report body when the payload omits body', async () => {
    get.mockResolvedValue({ data: progress('completed') });
    renderCard();

    const card = await screen.findByRole('region', { name: 'Worker progress' });
    expect(card.querySelector('.sa-progress-summary')).toHaveTextContent('summary-completed');
    expect(card.querySelector('.sa-progress-body')).not.toBeInTheDocument();
  });

  it.each([undefined, null] as const)(
    'renders no report body when the payload body is %s',
    async (body) => {
      get.mockResolvedValue({ data: { ...progress('completed'), body } });
      renderCard();

      const card = await screen.findByRole('region', { name: 'Worker progress' });
      expect(card.querySelector('.sa-progress-summary')).toHaveTextContent('summary-completed');
      expect(card.querySelector('.sa-progress-body')).not.toBeInTheDocument();
    },
  );

  it('hides the card on 404 and does not show an error', async () => {
    get.mockRejectedValue(refused(404));
    renderCard();
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(screen.queryByRole('region', { name: 'Worker progress' })).not.toBeInTheDocument();
    expect(screen.queryByText(/Ticket not found/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Could not/)).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each(['completed', 'failed', 'cancelled'] as const)(
    'stops polling once %s is reached',
    async (phase) => {
      vi.useFakeTimers();
      get.mockResolvedValueOnce({ data: progress('running') }).mockResolvedValue({ data: progress(phase) });
      renderCard();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(get).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      // Query notification is a 0ms timer queued at the current clock, which a
      // zero advance does not run.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(get).toHaveBeenCalledTimes(2);
      expect(screen.getByText(PHASE_LABEL[phase])).toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_000);
      });
      expect(get).toHaveBeenCalledTimes(2);
    },
  );

  it('pauses while the document is hidden and resumes when it is visible', async () => {
    vi.useFakeTimers();
    let visibility: DocumentVisibilityState = 'visible';
    const spy = vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
    try {
      get.mockResolvedValue({ data: progress('running') });
      renderCard();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(get).toHaveBeenCalledTimes(1);

      visibility = 'hidden';
      act(() => {
        document.dispatchEvent(new Event('visibilitychange'));
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_000);
      });
      expect(get).toHaveBeenCalledTimes(1);

      visibility = 'visible';
      await act(async () => {
        document.dispatchEvent(new Event('visibilitychange'));
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(get).toHaveBeenCalledTimes(2);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      expect(get).toHaveBeenCalledTimes(3);

      visibility = 'hidden';
      act(() => {
        document.dispatchEvent(new Event('visibilitychange'));
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_000);
      });
      expect(get).toHaveBeenCalledTimes(3);
    } finally {
      spy.mockRestore();
    }
  });

  it('does not keep polling after unmount', async () => {
    vi.useFakeTimers();
    get.mockResolvedValue({ data: progress('queued') });
    const view = renderCard();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(get).toHaveBeenCalledTimes(1);
    view.unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(get).toHaveBeenCalledTimes(1);
  });

  it('keeps a slow in-flight response instead of cancelling it on the next poll', async () => {
    vi.useFakeTimers();
    let attempt = 0;
    get.mockImplementation(() => {
      attempt += 1;
      if (attempt === 1) return Promise.resolve({ data: progress('running') });
      return new Promise((resolve) => {
        setTimeout(() => resolve({ data: progress('completed') }), 6_000);
      });
    });
    renderCard();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(get).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(get).toHaveBeenCalledTimes(2);
    expect(screen.getByText('Running')).toBeInTheDocument();

    // The completed payload is still one second out. The next interval must
    // not replace it; cancelling here is what used to drop every response.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(get).toHaveBeenCalledTimes(2);
    expect(screen.getByText('Running')).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(25_000);
    });
    expect(screen.getByText('Completed')).toBeInTheDocument();
    expect(screen.getByText('summary-completed')).toBeInTheDocument();
    expect(get).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(get).toHaveBeenCalledTimes(2);
  });

  it('abandons a progress request that never returns so the next poll still runs', async () => {
    vi.useFakeTimers();
    let attempt = 0;
    get.mockImplementation(() => {
      attempt += 1;
      if (attempt === 1) return Promise.resolve({ data: progress('running') });
      if (attempt === 2) return new Promise(() => undefined);
      return Promise.resolve({ data: progress('completed') });
    });
    renderCard();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(get).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(get).toHaveBeenCalledTimes(2);
    expect(screen.getByText('Running')).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(screen.getByText('Running')).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(get).toHaveBeenCalledTimes(3);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText('Completed')).toBeInTheDocument();
  });

  it('keeps polling after the first request and its production retry both time out', async () => {
    vi.useFakeTimers();
    let attempt = 0;
    get.mockImplementation(() => {
      attempt += 1;
      if (attempt < 3) return new Promise(() => undefined);
      return Promise.resolve({ data: progress('completed') });
    });
    renderCard(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(get).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('region', { name: 'Worker progress' })).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(get).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    // The timeout's error render is one timer turn later. That render arms the interval.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(get).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('region', { name: 'Worker progress' })).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(get).toHaveBeenCalledTimes(3);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(screen.getByText('Completed')).toBeInTheDocument();
  });
});

describe('task page', () => {
  const ticket = {
    ticket_id: 't-1',
    user_id: 'u-1',
    workspace_id: 'ws-1',
    title: 'Fix the save button',
    scope: 'Change the label',
    status: 'running',
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

  function routeGet(overrides: Record<string, unknown> = {}, phase: keyof typeof STEPS = 'running') {
    get.mockImplementation(async (url: string) => {
      if (url.endsWith('/progress')) return { data: progress(phase) };
      if (url.endsWith('/activity')) {
        return {
          data: {
            ticket_id: 't-1',
            events: [
              { event_id: 'e1', kind: 'created', at: '2026-11-03T12:00:00Z', summary: 'Ticket brief', detail: '', state: '' },
              { event_id: 'e2', kind: 'instruction', at: '2026-11-03T12:01:00Z', summary: 'Keep the label short', detail: '', state: '' },
              { event_id: 'e3', kind: 'report', at: '2026-11-03T12:02:00Z', summary: 'Changed the label', detail: '', state: 'ready' },
            ],
          },
        };
      }
      if (url.includes('/sources')) return { data: { sources: [] } };
      if (url.includes('/schedules')) return { data: { schedules: [] } };
      if (url.includes('/tickets/')) return { data: { ...ticket, ...overrides } };
      return { data: {} };
    });
  }

  function renderPage() {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    return render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/tasks/t-1']}>
          <Routes>
            <Route path="/tasks/:ticketId" element={<TaskPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it('lays out the header, conversation, progress, report and delivery check in the mockup order', async () => {
    routeGet();
    renderPage();

    const card = await screen.findByRole('region', { name: 'Worker progress' });
    const title = await screen.findByRole('heading', { name: 'Fix the save button' });
    const conversation = screen.getByRole('region', { name: 'Conversation' });
    const report = screen.getByRole('region', { name: 'Report' });
    const delivery = screen.getByRole('region', { name: 'Git delivery check' });
    expect(card).toHaveTextContent('Running');
    expect(card).not.toHaveTextContent('summary-running');
    const after = (a: Node, b: Node) => Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    expect(after(title, conversation)).toBe(true);
    expect(after(conversation, card)).toBe(true);
    expect(after(card, report)).toBe(true);
    expect(after(report, delivery)).toBe(true);
    expect(card).not.toHaveTextContent(LEAK_TRANSCRIPT);
    expect(card).not.toHaveTextContent(LEAK_TOOL);
    // The running Ticket wears the running glyph in the header.
    expect(screen.getByTestId('ticket-id').parentElement?.querySelector('.sa-state--running')).toBeInTheDocument();
  });

  it('renders the activity as conversation turns: router, operator and worker', async () => {
    routeGet();
    renderPage();

    const conversation = await screen.findByRole('region', { name: 'Conversation' });
    await waitFor(() => expect(conversation.querySelectorAll('.tn-ticket-turn')).toHaveLength(3));
    const sides = [...conversation.querySelectorAll('.tn-ticket-turn')].map((el) => el.className);
    expect(sides[0]).toContain('tn-ticket-turn--router');
    expect(sides[1]).toContain('tn-ticket-turn--user');
    expect(sides[2]).toContain('tn-ticket-turn--worker');
    expect(conversation).toHaveTextContent('Keep the label short');
    expect(conversation.querySelector('[data-transcript]')).toHaveAttribute('tabindex', '0');
  });

  it('shows the latest worker report and a read-only delivery check from the Ticket fields', async () => {
    routeGet({
      status: 'completed',
      worktree_path: '/srv/conductor/dispatch/ws/review-merge',
      worktree_branch: 'line/review-merge',
      worktree_base_sha: 'b0f4d2189a7c3e11',
      worktree_host: 'server',
      worktree_cleanup_pending: false,
      reports: [
        { report_id: 'r-1', state: 'waiting', summary: 'First pass', question: 'Which branch?', created_at: '2026-11-03T12:01:00Z' },
        { report_id: 'r-2', state: 'ready', summary: 'Landed the label', body: 'Full body text', question: null, created_at: '2026-11-03T12:04:00Z' },
      ],
    }, 'completed');
    renderPage();

    const report = await screen.findByTestId('ticket-report');
    expect(report).toHaveTextContent('Landed the label');
    expect(report).toHaveTextContent('Full body text');
    expect(report).not.toHaveTextContent('First pass');
    const delivery = screen.getByTestId('ticket-delivery');
    const row = (key: string) => delivery.querySelector(`[data-check="${key}"]`) as HTMLElement;
    expect(row('branch')).toHaveTextContent('line/review-merge');
    expect(row('branch').querySelector('dd')).toHaveClass('tn-ticket-code');
    expect(row('base')).toHaveTextContent('b0f4d2189a7c');
    expect(row('workspace')).toHaveTextContent('/srv/conductor/dispatch/ws/review-merge');
    expect(row('gitCheck')).toHaveAttribute('data-mark', 'ok');
    expect(row('decision')).toHaveTextContent('delivered');
    expect(row('decision')).toHaveAttribute('data-mark', 'ok');
    // Read-only: the check offers no control of its own.
    expect(delivery.querySelectorAll('button, input, textarea, select')).toHaveLength(0);
  });

  it('flags a runtime the delivery check cannot inspect and an empty report', async () => {
    routeGet({
      reports: [{ report_id: 'r-1', state: 'ready', summary: 'Done', question: null, git_check: 'unsupported_runtime', created_at: '2026-11-03T12:04:00Z' }],
    });
    renderPage();
    const delivery = await screen.findByTestId('ticket-delivery');
    const row = delivery.querySelector('[data-check="gitCheck"]');
    expect(row).toHaveAttribute('data-mark', 'hold');
    expect(row).toHaveTextContent('this runtime cannot be checked');
    expect(delivery.querySelector('[data-check="branch"]')).toHaveTextContent('not recorded');
  });
});
