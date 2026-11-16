import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation, useParams } from 'react-router';
import i18n from 'i18next';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { assertNoNamespaceCollision, assertParity } from '@/lib/featureLocales';
import tnRunsEn from '../locales/tnRuns.en-US.json';
import tnRunsZh from '../locales/tnRuns.zh-CN.json';

const apiMocks = vi.hoisted(() => ({
  listAutonomyTickets: vi.fn(),
  listAutonomySources: vi.fn(),
  getAutonomyTicket: vi.fn(),
  getAutonomyTicketRaw: vi.fn(),
}));

vi.mock('@/api/autonomy', () => apiMocks);

import RunsPage from '../RunsPage';

const ago = (seconds: number) => new Date(Date.now() - seconds * 1000).toISOString();

type Overrides = Record<string, unknown>;
const ticket = (id: string, overrides: Overrides = {}) => ({
  ticket_id: id,
  user_id: 'u-1',
  workspace_id: 'ws-1',
  title: `Run ${id}`,
  scope: '',
  status: 'running',
  mandate: {},
  origin_trigger_id: null,
  origin_thread_key: 'github:owner/repo#1',
  router_thread_id: null,
  worker_thread_id: `thread-${id}`,
  worker_run_id: null,
  worker_provider: 'kimi',
  worker_model: 'kimi-k3',
  attempts: 1,
  attention: null,
  summary: '',
  error: '',
  created_at: ago(3600),
  updated_at: ago(120),
  started_at: ago(30),
  ended_at: null,
  ...overrides,
});

/** One Ticket per terminal-native state, plus one that never reached a Worker. */
const ALL_STATES = [
  ticket('t-queued', { status: 'queued', started_at: null, title: 'Queued run' }),
  ticket('t-running', { status: 'running', title: 'Running run' }),
  ticket('t-blocked', { status: 'blocked', title: 'Blocked run', started_at: ago(400), ended_at: ago(153) }),
  // A worker question wins over the running status.
  ticket('t-needs', {
    status: 'running',
    title: 'Needs-you run',
    attention: { question: 'Merge the two reports?' },
  }),
  ticket('t-failed', { status: 'failed', title: 'Failed run', started_at: ago(4000), ended_at: ago(280) }),
  ticket('t-completed', {
    status: 'completed',
    title: 'Completed run',
    mandate: { schedule_id: 's-1' },
    started_at: '2026-11-13T10:00:00Z',
    ended_at: '2026-11-13T10:04:07Z',
  }),
  ticket('t-archived', { status: 'cancelled', title: 'Archived run', origin_thread_key: 'manual:abc' }),
  ticket('t-noworker', { status: 'queued', title: 'No worker yet', worker_thread_id: null }),
];

function TaskProbe() {
  const { ticketId } = useParams();
  return <p>task page {ticketId}</p>;
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.search}</output>;
}

function renderPage(entry = '/runs') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/runs" element={<><RunsPage /><LocationProbe /></>} />
          <Route path="/tasks/:ticketId" element={<TaskProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const runRows = () => Array.from(document.querySelectorAll<HTMLElement>('[data-tn-run]'));

beforeEach(async () => {
  await i18n.changeLanguage('en-US');
  apiMocks.listAutonomyTickets.mockReset().mockResolvedValue({ tickets: ALL_STATES });
  apiMocks.listAutonomySources.mockReset().mockResolvedValue({ sources: [] });
  apiMocks.getAutonomyTicket.mockReset();
  apiMocks.getAutonomyTicketRaw.mockReset().mockImplementation(async (id: string) => ({
    thread_id: `thread-${id}`,
    run_id: null,
    events: [{ id: `evt-${id}`, data: `{"ticket":"${id}"}` }],
    note: null,
  }));
});

afterEach(async () => {
  await i18n.changeLanguage('en-US');
});

describe('RunsPage', () => {
  it('renders every state as a glyph + word row and drops tickets without a Worker', async () => {
    renderPage();
    await screen.findByText('Completed run');
    const rows = runRows();
    expect(rows.map((r) => r.dataset.state)).toEqual([
      'queued',
      'running',
      'blocked',
      'needs-you',
      'failed',
      'completed',
      'archived',
    ]);
    const words = rows.map((r) => r.querySelector('.sa-state-word')?.textContent);
    expect(words).toEqual(['Queued', 'Running', 'Blocked', 'Needs you', 'Failed', 'Completed', 'Archived']);
    for (const row of rows) {
      const glyph = row.querySelector('.sa-state-glyph');
      expect(glyph?.textContent).toMatch(/^[○●▣◆✖✔⊘]$/);
      expect(glyph).toHaveAttribute('aria-hidden', 'true');
    }
    expect(screen.queryByText('No worker yet')).not.toBeInTheDocument();
    // The running glyph spins; nothing else does.
    expect(document.querySelectorAll('.sa-state-glyph--spin')).toHaveLength(1);
    // The worker question rides under its run.
    expect(within(rows[3]).getByText('Merge the two reports?')).toBeInTheDocument();
  });

  it('shows right-aligned tabular durations from started → ended', async () => {
    renderPage();
    await screen.findByText('Completed run');
    const duration = (id: string) =>
      document.querySelector(`[data-tn-run="${id}"] .tn-runs-duration`)?.textContent;
    expect(duration('t-completed')).toBe('4m 07s');
    expect(duration('t-blocked')).toBe('4m 07s');
    expect(duration('t-failed')).toBe('1h 02m');
    expect(duration('t-queued')).toBe('—');
    // Stopped without an end stamp: no duration, never a clock still counting.
    expect(duration('t-archived')).toBe('—');
    expect(duration('t-running')).toMatch(/^\d+s$/);
    for (const el of document.querySelectorAll('.tn-runs-duration')) {
      expect(el).toHaveClass('tn-runs-num');
    }
  });

  it('reads origin and worker into the row', async () => {
    renderPage();
    await screen.findByText('Completed run');
    const row = (id: string) => document.querySelector<HTMLElement>(`[data-tn-run="${id}"]`)!;
    expect(row('t-completed').querySelector('.tn-runs-origin')?.textContent).toBe('Schedule');
    expect(row('t-archived').querySelector('.tn-runs-origin')?.textContent).toBe('Manual');
    expect(row('t-running').querySelector('.tn-runs-origin')?.textContent).toBe('GitHub');
    expect(row('t-running').querySelector('.tn-runs-worker')?.textContent).toContain('kimi-k3');
  });

  it('selects the first run, writes ?task, and loads its raw log', async () => {
    renderPage();
    await screen.findByText('Completed run');
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('?task=t-queued'));
    const [first] = runRows();
    expect(first).toHaveAttribute('aria-current', 'true');
    expect(await screen.findByText(/evt-t-queued/)).toBeInTheDocument();
    expect(apiMocks.getAutonomyTicketRaw).toHaveBeenCalledWith('t-queued');
  });

  it('opens a run on click and on Enter', async () => {
    renderPage();
    await screen.findByText('Completed run');
    await userEvent.click(screen.getByText('Failed run'));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('?task=t-failed'));
    expect(document.querySelector('[data-tn-run="t-failed"]')).toHaveAttribute('aria-current', 'true');
    expect(await screen.findByText(/evt-t-failed/)).toBeInTheDocument();

    const archived = document.querySelector<HTMLElement>('[data-tn-run="t-archived"]')!;
    archived.focus();
    await userEvent.keyboard('{Enter}');
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('?task=t-archived'));
  });

  it('keeps one roving tab stop and moves it with j/k and the arrows', async () => {
    renderPage('/runs?task=t-blocked');
    await screen.findByText('Completed run');
    const rows = runRows();
    expect(rows.filter((r) => r.tabIndex === 0)).toEqual([rows[2]]);
    rows[2].focus();
    fireEvent.keyDown(rows[2], { key: 'j' });
    expect(document.activeElement).toBe(rows[3]);
    fireEvent.keyDown(rows[3], { key: 'ArrowDown' });
    expect(document.activeElement).toBe(rows[4]);
    fireEvent.keyDown(rows[4], { key: 'k' });
    fireEvent.keyDown(rows[3], { key: 'ArrowUp' });
    expect(document.activeElement).toBe(rows[2]);
    fireEvent.keyDown(rows[2], { key: 'j', ctrlKey: true });
    expect(document.activeElement).toBe(rows[2]);
    rows[0].focus();
    fireEvent.keyDown(rows[0], { key: 'k' });
    expect(document.activeElement).toBe(rows[0]);
    await waitFor(() => expect(runRows().filter((r) => r.tabIndex === 0)).toEqual([rows[0]]));
  });

  it('copies the raw log and opens the Task', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    renderPage('/runs?task=t-running');
    await screen.findByText(/evt-t-running/);
    await userEvent.click(screen.getByRole('button', { name: 'Copy raw' }));
    expect(writeText).toHaveBeenCalledWith('evt-t-running\n{"ticket":"t-running"}\n');
    await userEvent.click(screen.getByRole('button', { name: 'Open Task' }));
    expect(await screen.findByText('task page t-running')).toBeInTheDocument();
  });

  it('fetches the Ticket when the requested run is not in the list', async () => {
    apiMocks.getAutonomyTicket.mockResolvedValue(
      ticket('t-old', { title: 'An older run', worker_provider: 'codex', worker_model: 'gpt-x' }),
    );
    renderPage('/runs?task=t-old');
    expect(await screen.findByText('An older run')).toBeInTheDocument();
    expect(screen.getByText('codex · gpt-x')).toBeInTheDocument();
    expect(apiMocks.getAutonomyTicket).toHaveBeenCalledWith('t-old');
  });

  it('shows the empty and error states', async () => {
    apiMocks.listAutonomyTickets.mockResolvedValue({ tickets: [] });
    const { unmount } = renderPage();
    expect(await screen.findByText('No Worker has run yet.')).toBeInTheDocument();
    expect(screen.getByText('Select a run.')).toBeInTheDocument();
    unmount();

    apiMocks.listAutonomyTickets.mockRejectedValue(new Error('down'));
    renderPage();
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not read Worker runs. Check the connection and retry.',
    );
  });

  it('renders the tnRuns copy in zh-CN', async () => {
    await i18n.changeLanguage('zh-CN');
    renderPage('/runs?task=t-completed');
    expect(await screen.findByRole('heading', { name: '执行日志' })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: '复制原始记录' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '打开任务' })).toBeInTheDocument();
    expect(document.querySelector('[data-tn-run="t-completed"] .tn-runs-duration')?.textContent).toBe('4分07秒');
    expect(document.querySelector('[data-tn-run="t-completed"] .tn-runs-origin')?.textContent).toBe('定时');
  });
});

describe('tnRuns locales', () => {
  it('keeps en-US / zh-CN key parity', () => {
    expect(assertParity(tnRunsEn, tnRunsZh)).toEqual({ enOnly: [], zhOnly: [] });
  });

  it('tnRuns does not collide with the main catalog', () => {
    // Read from disk: the imported module object is the live i18n resource
    // store here, which registerFeatureLocales has already merged tnRuns into.
    const mainCatalog = JSON.parse(readFileSync(resolve(__dirname, '../../../locales/en-US.json'), 'utf8'));
    expect(() => assertNoNamespaceCollision('tnRuns', mainCatalog)).not.toThrow();
    expect(Object.keys(tnRunsEn)).toEqual(['tnRuns']);
    expect(Object.keys(tnRunsZh)).toEqual(['tnRuns']);
  });
});

describe('runs.css', () => {
  const css = readFileSync(resolve(__dirname, '../runs.css'), 'utf8');
  const tokens = JSON.parse(
    readFileSync(resolve(__dirname, '../../../../../docs/design/terminal-native/system/tokens.json'), 'utf8'),
  );

  it('uses only token hex values', () => {
    const allowed = new Set<string>();
    const walk = (v: unknown) => {
      if (typeof v === 'string') {
        for (const m of v.matchAll(/#[0-9a-fA-F]{6}\b/g)) allowed.add(m[0].toLowerCase());
      } else if (v && typeof v === 'object') Object.values(v).forEach(walk);
    };
    walk(tokens);
    const used = [...css.matchAll(/#[0-9a-fA-F]{3,8}\b/g)].map((m) => m[0].toLowerCase());
    expect(used.length).toBeGreaterThan(0);
    expect(used.filter((hex) => !allowed.has(hex))).toEqual([]);
  });

  it('keeps square geometry: no radius above radius.card and no pills', () => {
    for (const m of css.matchAll(/border-radius:\s*([^;]+);/g)) {
      expect(m[1]).toMatch(/var\(--radius-(none|control|card)/);
    }
    expect(css).not.toMatch(/999px/);
  });

  it('right-aligns tabular durations and never shrinks the state column', () => {
    expect(css).toMatch(/\.tn-runs-num\s*\{[^}]*justify-self:\s*end/);
    expect(css).toMatch(/\.tn-runs-num\s*\{[^}]*font-variant-numeric:\s*tabular-nums/);
    expect(css).toMatch(/\.tn-runs-state\s*\{[^}]*white-space:\s*nowrap/);
    expect(css).toMatch(/\.tn-runs-trunc\s*\{[^}]*text-overflow:\s*ellipsis/);
  });

  it('rings a focused row with the 2px ink.focus outline', () => {
    expect(css).toMatch(/\.tn-runs-run:focus-visible\s*\{[^}]*outline:\s*var\(--border-focus, 2px\) solid var\(--ink-focus/);
  });

  it('declares no transition or animation of its own', () => {
    expect(css).not.toMatch(/transition|animation/);
  });
});
