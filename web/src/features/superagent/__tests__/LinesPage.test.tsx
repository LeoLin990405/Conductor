import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useParams } from 'react-router';
import { assertNoNamespaceCollision, assertParity } from '@/lib/featureLocales';
import tnLinesEn from '../locales/tnLines.en-US.json';
import tnLinesZh from '../locales/tnLines.zh-CN.json';

const apiMocks = vi.hoisted(() => ({
  getAutonomyTicketLines: vi.fn(),
}));

vi.mock('@/api/autonomy', () => ({ getAutonomyTicketLines: apiMocks.getAutonomyTicketLines }));

import LinesPage from '../LinesPage';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const recent = (minutesAgo: number) => new Date(Date.now() - minutesAgo * 60_000).toISOString();

const LINES = {
  lines: [{
    line: {
      line_id: 'line-1', user_id: 'user-1', name: 'ob-tickets', branch: 'feat/ob-tickets',
      worktree_path: '/Users/you/conductor-wt/ob-tickets', status: 'active',
      created_at: recent(120), updated_at: recent(5),
    },
    tickets: [
      {
        ticket_id: 'ticket-1', title: 'Fix the save button', status: 'running',
        owner: 'm5-daemon', worker_provider: 'kimi', attempts: 1,
        updated_at: recent(3), last_activity_at: recent(1),
      },
      {
        ticket_id: 'ticket-2', title: 'Polish the empty state', status: 'completed',
        owner: 'cloud', worker_provider: '', attempts: 2,
        updated_at: recent(60), last_activity_at: recent(60),
      },
    ],
    ticket_count: 2,
    open_count: 1,
    last_activity_at: recent(1),
  }],
  ungrouped: {
    line: null,
    tickets: [
      {
        ticket_id: 'ticket-3', title: 'Triage inbox noise', status: 'waiting',
        owner: 'cloud', worker_provider: '', attempts: 1,
        updated_at: recent(10), last_activity_at: null,
      },
    ],
    ticket_count: 1,
    open_count: 1,
    last_activity_at: null,
  },
};

function TaskProbe() {
  const { ticketId } = useParams();
  return <p>task page {ticketId}</p>;
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/lines']}>
        <Routes>
          <Route path="/lines" element={<LinesPage />} />
          <Route path="/tasks/:ticketId" element={<TaskProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The rows of the list in DOM order (line rows and ticket rows). */
const rows = () => Array.from(document.querySelectorAll<HTMLElement>('[data-tn-row]'));

beforeEach(() => {
  vi.clearAllMocks();
  apiMocks.getAutonomyTicketLines.mockResolvedValue(LINES);
});

describe('tnLines locales', () => {
  it('keeps en-US / zh-CN key parity', () => {
    expect(assertParity(tnLinesEn, tnLinesZh)).toEqual({ enOnly: [], zhOnly: [] });
  });

  it('holds exactly the tnLines namespace and collides with no main-catalog namespace', () => {
    expect(Object.keys(tnLinesEn)).toEqual(['tnLines']);
    expect(Object.keys(tnLinesZh)).toEqual(['tnLines']);
    // Read the catalog from disk: the test setup hands the imported en-US object
    // to i18n, and registerFeatureLocales deep-merges tnLines into it in place.
    const mainCatalog = JSON.parse(readFileSync(resolve(__dirname, '../../../locales/en-US.json'), 'utf8'));
    expect(() => assertNoNamespaceCollision('tnLines', mainCatalog)).not.toThrow();
    expect(() => assertNoNamespaceCollision('tnLines', { tnLines: {} })).toThrow();
  });
});

describe('LinesPage', () => {
  it('shows each line with its branch, worktree and open/total counts', async () => {
    renderPage();

    expect(await screen.findByRole('heading', { name: 'ob-tickets' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name: 'Lines' })).toBeInTheDocument();
    const branch = screen.getByText('feat/ob-tickets');
    expect(branch.tagName).toBe('CODE');
    const worktree = screen.getByText('/Users/you/conductor-wt/ob-tickets');
    expect(worktree.tagName).toBe('CODE');
    expect(screen.getByText('1 open · 2 total')).toBeInTheDocument();
  });

  it('puts the line state glyph + word and its worker on the line row', async () => {
    renderPage();

    const heading = await screen.findByRole('heading', { name: 'ob-tickets' });
    const lineRow = within(heading).getByRole('button');
    // Rolled up from a running Ticket; the worker is the most recently active one.
    expect(lineRow.querySelector('.sa-state--running .sa-state-glyph')).toHaveTextContent('●');
    expect(within(lineRow).getByText('Running')).toBeInTheDocument();
    expect(within(lineRow).getByText('kimi')).toBeInTheDocument();
    expect(lineRow).toHaveAccessibleDescription(/Running.*kimi.*feat\/ob-tickets.*1 open · 2 total/);
  });

  it('shows each ticket with its state glyph + word and owner', async () => {
    renderPage();

    const running = (await screen.findByText('Fix the save button')).closest('a')!;
    expect(running).toHaveAttribute('href', '/tasks/ticket-1');
    expect(within(running).getByText('Running')).toBeInTheDocument();
    expect(running.querySelector('.sa-state-glyph')).toHaveTextContent('●');
    expect(within(running).getByText('m5-daemon · kimi')).toBeInTheDocument();

    const done = screen.getByText('Polish the empty state').closest('a')!;
    expect(within(done).getByText('Completed')).toBeInTheDocument();
    expect(done.querySelector('.sa-state-glyph')).toHaveTextContent('✔');
  });

  it('renders ungrouped tickets under the Ungrouped heading', async () => {
    renderPage();

    expect(await screen.findByRole('heading', { name: 'Ungrouped' })).toBeInTheDocument();
    const triage = screen.getByText('Triage inbox noise').closest('a')!;
    expect(within(triage).getByText('Needs you')).toBeInTheDocument();
    expect(screen.getByText('1 open · 1 total')).toBeInTheDocument();
  });

  it('only reads the lines endpoint and makes no mutation calls', async () => {
    renderPage();

    expect(await screen.findByText('Fix the save button')).toBeInTheDocument();
    expect(apiMocks.getAutonomyTicketLines).toHaveBeenCalledTimes(1);
    expect(apiMocks.getAutonomyTicketLines).toHaveBeenCalledWith(false);
    expect(Object.keys(apiMocks)).toEqual(['getAutonomyTicketLines']);
  });

  it('Refresh re-reads the lines endpoint', async () => {
    renderPage();
    await screen.findByText('Fix the save button');
    await userEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(apiMocks.getAutonomyTicketLines).toHaveBeenCalledTimes(2));
    expect(apiMocks.getAutonomyTicketLines).toHaveBeenLastCalledWith(false);
  });

  it('shows an error state when the lines cannot be read', async () => {
    apiMocks.getAutonomyTicketLines.mockRejectedValue(new Error('offline'));
    renderPage();
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not read the Ticket lines. Try Refresh.');
  });

  it('shows an empty state when there are no lines and no ungrouped tickets', async () => {
    apiMocks.getAutonomyTicketLines.mockResolvedValue({ lines: [], ungrouped: null });
    renderPage();
    expect(await screen.findByText('No Ticket lines yet.')).toBeInTheDocument();
  });

  it('collapses and expands a line from its row', async () => {
    renderPage();
    const heading = await screen.findByRole('heading', { name: 'ob-tickets' });
    const lineRow = within(heading).getByRole('button');
    expect(lineRow).toHaveAttribute('aria-expanded', 'true');
    await userEvent.click(lineRow);
    expect(lineRow).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Fix the save button')).not.toBeInTheDocument();
    await userEvent.click(lineRow);
    expect(screen.getByText('Fix the save button')).toBeInTheDocument();
  });

  it('opens a ticket from its row with Enter', async () => {
    renderPage();
    const row = (await screen.findByText('Fix the save button')).closest('a')!;
    row.focus();
    await userEvent.keyboard('{Enter}');
    expect(await screen.findByText('task page ticket-1')).toBeInTheDocument();
  });
});

describe('LinesPage j/k row motion', () => {
  it('is one tab stop: only the first row is tabbable until another row takes focus', async () => {
    renderPage();
    await screen.findByText('Fix the save button');
    const all = rows();
    expect(all).toHaveLength(5); // 2 line rows + 3 ticket rows
    expect(all.map((r) => r.tabIndex)).toEqual([0, -1, -1, -1, -1]);
    all[2].focus();
    await waitFor(() => expect(rows().map((r) => r.tabIndex)).toEqual([-1, -1, 0, -1, -1]));
  });

  it('j moves to the next row and k to the previous one while the list has focus', async () => {
    renderPage();
    await screen.findByText('Fix the save button');
    const [line1, t1, t2, ungrouped, t3] = rows();
    line1.focus();

    fireEvent.keyDown(line1, { key: 'j' });
    expect(document.activeElement).toBe(t1);
    fireEvent.keyDown(t1, { key: 'j' });
    expect(document.activeElement).toBe(t2);
    fireEvent.keyDown(t2, { key: 'j' });
    expect(document.activeElement).toBe(ungrouped);
    fireEvent.keyDown(ungrouped, { key: 'j' });
    expect(document.activeElement).toBe(t3);
    // Clamped at the end.
    fireEvent.keyDown(t3, { key: 'j' });
    expect(document.activeElement).toBe(t3);

    fireEvent.keyDown(t3, { key: 'k' });
    expect(document.activeElement).toBe(ungrouped);
    fireEvent.keyDown(ungrouped, { key: 'k' });
    fireEvent.keyDown(t2, { key: 'k' });
    fireEvent.keyDown(t1, { key: 'k' });
    expect(document.activeElement).toBe(line1);
    // Clamped at the start.
    fireEvent.keyDown(line1, { key: 'k' });
    expect(document.activeElement).toBe(line1);
  });

  it('works with real key presses through userEvent', async () => {
    renderPage();
    await screen.findByText('Fix the save button');
    const [line1, t1] = rows();
    line1.focus();
    await userEvent.keyboard('j');
    expect(document.activeElement).toBe(t1);
    await userEvent.keyboard('k');
    expect(document.activeElement).toBe(line1);
  });

  it('does nothing when focus is outside the list', async () => {
    renderPage();
    await screen.findByText('Fix the save button');
    const refresh = screen.getByRole('button', { name: 'Refresh' });
    refresh.focus();
    await userEvent.keyboard('j');
    expect(document.activeElement).toBe(refresh);

    (document.activeElement as HTMLElement).blur();
    await userEvent.keyboard('j');
    expect(document.activeElement).toBe(document.body);
  });

  it('ignores j/k with a modifier held', async () => {
    renderPage();
    await screen.findByText('Fix the save button');
    const [line1] = rows();
    line1.focus();
    for (const mod of ['ctrlKey', 'metaKey', 'altKey'] as const) {
      fireEvent.keyDown(line1, { key: 'j', [mod]: true });
      expect(document.activeElement).toBe(line1);
    }
  });

  it('skips the rows of a collapsed line', async () => {
    renderPage();
    const heading = await screen.findByRole('heading', { name: 'ob-tickets' });
    const lineRow = within(heading).getByRole('button');
    await userEvent.click(lineRow);
    const [line1, ungrouped] = rows();
    expect(line1).toBe(lineRow);
    fireEvent.keyDown(line1, { key: 'j' });
    expect(document.activeElement).toBe(ungrouped);
  });
});

describe('line roll-ups', () => {
  const group = (id: string, statuses: string[], extra: Partial<{ open: number; archived: boolean }> = {}) => ({
    line: { ...LINES.lines[0].line, line_id: id, name: id, status: extra.archived ? 'archived' : 'active' },
    tickets: statuses.map((status, i) => ({
      ...LINES.lines[0].tickets[0],
      ticket_id: `${id}-t${i}`,
      status,
      worker_provider: i === 0 ? 'old' : 'new',
      last_activity_at: recent(i === 0 ? 50 : 2),
    })),
    ticket_count: statuses.length,
    open_count: extra.open ?? statuses.length,
    last_activity_at: null,
  });

  const lineRowOf = async (name: string) =>
    within(await screen.findByRole('heading', { name })).getByRole('button');

  it('needs-you beats failed beats running beats queued; nothing open is completed', async () => {
    apiMocks.getAutonomyTicketLines.mockResolvedValue({
      lines: [
        group('l-needs', ['running', 'waiting', 'failed']),
        group('l-failed', ['running', 'failed']),
        group('l-running', ['queued', 'running']),
        group('l-queued', ['queued']),
        group('l-done', ['completed'], { open: 0 }),
        group('l-archived', ['running'], { archived: true }),
      ],
      ungrouped: null,
    });
    renderPage();
    const expected: Record<string, string> = {
      'l-needs': 'needs-you',
      'l-failed': 'failed',
      'l-running': 'running',
      'l-queued': 'queued',
      'l-done': 'completed',
      'l-archived': 'archived',
    };
    for (const [name, state] of Object.entries(expected)) {
      const row = await lineRowOf(name);
      expect(row.querySelector('.tn-lines-state .sa-state'), name).toHaveClass(`sa-state--${state}`);
    }
  });

  it('the line worker is the most recently active Ticket provider; none says so', async () => {
    apiMocks.getAutonomyTicketLines.mockResolvedValue({
      lines: [group('l-two', ['running', 'running']), { ...group('l-empty', []), open_count: 0 }],
      ungrouped: null,
    });
    renderPage();
    expect(within(await lineRowOf('l-two')).getByText('new')).toBeInTheDocument();
    const empty = await lineRowOf('l-empty');
    expect(within(empty).getByText('no worker')).toBeInTheDocument();
    expect(screen.getByText('No Tickets on this line.')).toBeInTheDocument();
  });
});

describe('lines.css', () => {
  const css = readFileSync(resolve(__dirname, '../lines.css'), 'utf8');
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

  it('truncates long names and never shrinks the state column', () => {
    expect(css).toMatch(/\.tn-lines-trunc\s*\{[^}]*text-overflow:\s*ellipsis/);
    expect(css).toMatch(/\.tn-lines-state\s*\{[^}]*white-space:\s*nowrap/);
  });

  it('rings a focused row with the 2px ink.focus outline', () => {
    expect(css).toMatch(/\.tn-lines-row:focus-visible\s*\{[^}]*outline:\s*var\(--border-focus, 2px\) solid var\(--ink-focus/);
  });
});
