import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { userLocalStorage } from '@/lib/userStorage';
import {
  dagLayers,
  forgetDispatch,
  lineVerdict,
  readDispatchHistory,
  providerOf,
  rememberDispatch,
  reviewedCommit,
  statusMark,
  timelineOf,
  verdictOf,
  verdictRows,
} from '../model';
import DispatchDetailPage from '../DispatchDetailPage';
import DispatchListPage from '../DispatchListPage';

const listActiveDispatches = vi.hoisted(() => vi.fn());
const getDispatch = vi.hoisted(() => vi.fn());
const startDispatch = vi.hoisted(() => vi.fn());
const approveDispatch = vi.hoisted(() => vi.fn());
const rejectDispatch = vi.hoisted(() => vi.fn());
const reassignDispatchLine = vi.hoisted(() => vi.fn());
const getProviderCatalog = vi.hoisted(() => vi.fn());

vi.mock('@/api/dispatch', () => ({
  listActiveDispatches,
  getDispatch,
  startDispatch,
  approveDispatch,
  rejectDispatch,
  reassignDispatchLine,
  getProviderCatalog,
}));

const CATALOG = {
  byModel: { 'kimi-k3-coding': 'kimi', codex: 'codex', 'gpt-5.6-terra': 'openai' },
  providers: ['kimi', 'codex', 'openai'],
};

const FIXTURE = {
  id: 'd3adbeef',
  goal: 'Add a /healthz endpoint with tests',
  state: 'APPROVAL',
  progress_pct: 75,
  integrate_branch: 'integrate/auto-dispatch-d3adbeef',
  message: 'G5 passed. Awaiting human approval.',
  approval_token: 'tok-1',
  lines: [
    {
      id: 'line-0',
      name: 'api',
      status: 'PASSED',
      assigned_model: 'kimi-k3-coding',
      branch: 'line/line-0',
      base_commit: '3419e39e',
      depends_on: [] as string[],
      write_scope: ['src/server/health.py'],
      acceptance: ['pytest', 'human'],
      task: 'Add GET /healthz',
    },
  ],
  gates: {
    lines: {
      'line-0': {
        phase: 'awaiting_human',
        fix_round: 1,
        author: 'kimi-k3-coding',
        primary_reviewer: 'codex',
        gates: {
          G0: { result: 'pass', reason: 'planner_output_valid' },
          primary_review: { result: 'pass', verdict: 'merge', reviewer: 'codex', blocker_count: 0 },
          G6: { result: 'pending', reason: 'awaiting_human' },
        },
        events: [{ from_stage: 'G2', to_stage: 'G5', result: 'pass', reason: 'g5_passed' }],
        machine: { primary_verdict: 'approve' },
      },
    },
    approval: { token_present: true },
  },
};

/** The same dispatch with a second line, for the per-line refusals. */
const TWO_LINE_FIXTURE = {
  ...FIXTURE,
  lines: [
    FIXTURE.lines[0],
    {
      id: 'line-1',
      name: 'ui',
      status: 'PASSED',
      assigned_model: 'gpt-5.6-terra',
      branch: 'line/line-1',
      depends_on: ['line-0'],
    },
  ],
};

function w0Fixture(): { gates: { lines: Record<string, Parameters<typeof timelineOf>[0]> } } {
  const candidates = [
    resolve(process.cwd(), '../tests/fixtures/dispatch_gates_v1.json'),
    resolve(process.cwd(), 'tests/fixtures/dispatch_gates_v1.json'),
  ];
  const found = candidates.find((path) => existsSync(path));
  if (!found) throw new Error(`dispatch fixture not found from ${process.cwd()}`);
  return JSON.parse(readFileSync(found, 'utf8'));
}

/** The current query string, so a test can see what the page left in the URL. */
function SearchProbe() {
  const location = useLocation();
  return <output data-testid="search">{location.search}</output>;
}

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <SearchProbe />
        <Routes>
          <Route path="/dispatch" element={<DispatchListPage />} />
          <Route path="/dispatch/:dispatchId" element={<DispatchDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('dispatch pages', () => {
  beforeEach(() => {
    userLocalStorage.removeByPrefix('dispatch.history');
    getProviderCatalog.mockResolvedValue(CATALOG);
    listActiveDispatches.mockResolvedValue([]);
    getDispatch.mockResolvedValue(null);
    startDispatch.mockResolvedValue({ id: 'new-one', goal: 'g', state: 'SUBMITTED' });
    approveDispatch.mockResolvedValue(undefined);
    rejectDispatch.mockResolvedValue(undefined);
    reassignDispatchLine.mockResolvedValue(FIXTURE);
  });

  afterEach(() => {
    vi.useRealTimers();
    userLocalStorage.removeByPrefix('dispatch.history');
  });

  /* ---------------- the shared rules ---------------- */

  it('layers a dependency and warns on a cycle', () => {
    expect(dagLayers([{ id: 'a' }, { id: 'c', depends_on: ['a', 'b'] }]).layers).toEqual([['a'], ['c']]);
    expect(dagLayers([{ id: 'c', depends_on: ['a', 'b'] }]).warnings[0]).toContain('⚠ missing dep');
    expect(dagLayers([{ id: 'a', depends_on: ['a'] }]).warnings.some((item) => item.includes('⚠ cycle'))).toBe(true);
  });

  it('prints a glyph and a word for a state and a verdict', () => {
    expect(statusMark('APPROVAL')).toEqual({ id: 'needs-you', glyph: '◆', tone: 'hold' });
    expect(statusMark('awaiting_human')).toEqual({ id: 'needs-you', glyph: '◆', tone: 'hold' });
    expect(statusMark('RUNNING')).toEqual({ id: 'running', glyph: '●', tone: 'tempo' });
    expect(statusMark('COMPLETE')).toEqual({ id: 'completed', glyph: '✔', tone: 'ok' });
    expect(lineVerdict({ gates: { primary_review: { verdict: 'merge' } } })).toEqual({
      id: 'merge',
      glyph: '✔',
      tone: 'ok',
    });
    expect(lineVerdict({ gates: { primary_review: { verdict: 'block' } } }).glyph).toBe('✖');
    expect(lineVerdict(null).glyph).toBe('○');
  });

  it('names the provider of a model from the catalog and never guesses one', () => {
    expect(providerOf('kimi-k3-coding', CATALOG)).toBe('kimi');
    expect(providerOf('gpt-5.6-terra', CATALOG)).toBe('openai');
    // `gpt` is not a provider the catalog knows, so the model stays unnamed.
    expect(providerOf('gpt-5.6-terra', { byModel: {}, providers: ['kimi'] })).toBe('');
    expect(providerOf('', CATALOG)).toBe('');
  });

  it('names the reviewed revision the backend recorded, and nothing else', () => {
    expect(reviewedCommit({ last_review_blockers: { head: 'bbbbbbbb' } })).toBe('bbbbbbbb');
    expect(reviewedCommit({ gates: { G2: { revision: 'cccccccc' } } })).toBe('cccccccc');
    // A line with no recorded revision has none: the branch base is a
    // different commit and is never named here.
    expect(reviewedCommit({})).toBe('');
    expect(reviewedCommit(null)).toBe('');
  });

  /* ---------------- the list page ---------------- */

  it('lists the active dispatches with a glyph and a word', async () => {
    listActiveDispatches.mockResolvedValue([
      {
        id: '7f3a91c2',
        goal: 'Rebuild the dispatch list',
        state: 'APPROVAL',
        progress_pct: 75,
        lines: [{ id: 'line-0' }],
      },
    ]);

    renderAt('/dispatch');

    expect(await screen.findByText('Rebuild the dispatch list')).toBeInTheDocument();
    expect(screen.getByText('◆')).toBeInTheDocument();
    expect(screen.getByText('needs you')).toBeInTheDocument();
    expect(screen.getByText('75%')).toBeInTheDocument();
    expect(screen.getByText('1 line')).toBeInTheDocument();
    expect(screen.getByText('1 dispatch')).toBeInTheDocument();
  });

  it('starts a dispatch and opens the id the backend returned', async () => {
    const user = userEvent.setup();
    getDispatch.mockImplementation(async (id: string) =>
      id === 'new-one' ? { id: 'new-one', goal: 'ship the dispatch page', state: 'SUBMITTED' } : null,
    );
    renderAt('/dispatch');

    await user.type(screen.getByLabelText('Goal'), 'ship the dispatch page');
    await user.click(screen.getByRole('button', { name: 'Start' }));

    await waitFor(() => expect(startDispatch).toHaveBeenCalledWith('ship the dispatch page'));
    // The dispatch the backend started is the page that opens.
    expect(await screen.findByRole('heading', { name: 'new-one' })).toBeInTheDocument();
    expect(getDispatch).toHaveBeenCalledWith('new-one');
    // The started dispatch is in the history, newest first.
    expect(readDispatchHistory()[0]).toBe('new-one');
  });

  it('shows the refusal the backend gave when a dispatch cannot start', async () => {
    startDispatch.mockRejectedValue({
      response: { status: 400, data: { detail: 'planner_disabled' } },
    });
    const user = userEvent.setup();
    renderAt('/dispatch');

    await user.type(screen.getByLabelText('Goal'), 'ship the dispatch page');
    await user.click(screen.getByRole('button', { name: 'Start' }));

    expect(await screen.findByText('planner_disabled')).toBeInTheDocument();
  });

  it('reads the history of the dispatches the user has seen, one dispatch at a time', async () => {
    rememberDispatch('d3adbeef');
    getDispatch.mockImplementation(async (id: string) =>
      id === 'd3adbeef' ? { ...FIXTURE, id: 'd3adbeef' } : null,
    );

    renderAt('/dispatch');

    expect(await screen.findByText('d3adbeef')).toBeInTheDocument();
    expect(getDispatch).toHaveBeenCalledWith('d3adbeef');
    // The remembered dispatch shows the state it is in now.
    expect(screen.getByText('◆')).toBeInTheDocument();
    expect(screen.getByText('needs you')).toBeInTheDocument();
  });

  it('lists a started and a viewed dispatch in the history with the state each is in', async () => {
    const user = userEvent.setup();
    const rows: Record<string, object> = {
      'new-one': { id: 'new-one', goal: 'a started goal', state: 'SUBMITTED', lines: [] },
      d3adbeef: { ...FIXTURE, state: 'COMPLETE' },
    };
    getDispatch.mockImplementation(async (id: string) => rows[id] ?? null);

    // Viewed: opening the detail page is what puts the id in the history.
    const viewed = renderAt('/dispatch/d3adbeef');
    await screen.findByRole('heading', { name: 'd3adbeef' });
    viewed.unmount();
    expect(readDispatchHistory()).toEqual(['d3adbeef']);

    // Started: the start form remembers the id the backend returned.
    const started = renderAt('/dispatch');
    await user.type(screen.getByLabelText('Goal'), 'a started goal');
    await user.click(screen.getByRole('button', { name: 'Start' }));
    await screen.findByRole('heading', { name: 'new-one' });
    started.unmount();
    expect(readDispatchHistory()).toEqual(['new-one', 'd3adbeef']);

    renderAt('/dispatch');
    const history = screen.getByRole('region', { name: /history/i });
    const startedRow = (await within(history).findByRole('button', { name: 'new-one' })).closest('li');
    const viewedRow = (await within(history).findByRole('button', { name: 'd3adbeef' })).closest('li');
    expect(startedRow).toHaveTextContent('○');
    expect(startedRow).toHaveTextContent('queued');
    expect(viewedRow).toHaveTextContent('✔');
    expect(viewedRow).toHaveTextContent('completed');
  });

  it('forgets a dispatch the backend no longer serves', async () => {
    rememberDispatch('gone-one');
    getDispatch.mockResolvedValue(null);

    renderAt('/dispatch');

    await waitFor(() => expect(getDispatch).toHaveBeenCalledWith('gone-one'));
    expect(screen.queryByText('gone-one')).not.toBeInTheDocument();
  });

  it('opens a dispatch by id', async () => {
    const user = userEvent.setup();
    getDispatch.mockResolvedValue(FIXTURE);
    renderAt('/dispatch');

    await user.type(screen.getByLabelText('Dispatch id'), 'd3adbeef');
    await user.click(screen.getByRole('button', { name: 'Open' }));

    expect(await screen.findByRole('heading', { name: 'd3adbeef' })).toBeInTheDocument();
  });

  /* ---------------- the detail page ---------------- */

  it('shows every line with its provider and model', async () => {
    getDispatch.mockResolvedValue(FIXTURE);
    renderAt('/dispatch/d3adbeef');

    expect(await screen.findByRole('heading', { name: 'd3adbeef' })).toBeInTheDocument();
    expect(screen.getByText('kimi')).toBeInTheDocument();
    expect(screen.getByText('kimi-k3-coding')).toBeInTheDocument();
    // The reviewer is named twice: in the line card and in the verdict table.
    expect(screen.getAllByText('codex').length).toBeGreaterThan(0);
    expect(screen.getByText('line/line-0')).toBeInTheDocument();
  });

  it('shows a verdict as a glyph and a word', async () => {
    getDispatch.mockResolvedValue(FIXTURE);
    renderAt('/dispatch/d3adbeef');

    expect(await screen.findByText('L1')).toBeInTheDocument();
    // The verdict row and the verdict table both print the pair.
    expect(screen.getAllByText('✔').length).toBeGreaterThan(0);
    expect(screen.getAllByText('merge').length).toBeGreaterThan(0);
  });

  it('offers approve and reject only while the dispatch waits for a person', async () => {
    getDispatch.mockResolvedValue(FIXTURE);
    const { unmount } = renderAt('/dispatch/d3adbeef');

    expect(await screen.findByRole('button', { name: 'approve d3adbeef' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'reject d3adbeef' })).toBeInTheDocument();
    unmount();

    getDispatch.mockResolvedValue({ ...FIXTURE, state: 'MONITORING', gates: { lines: {} } });
    renderAt('/dispatch/d3adbeef');

    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'approve d3adbeef' })).not.toBeInTheDocument(),
    );
    expect(screen.queryByRole('button', { name: 'reject d3adbeef' })).not.toBeInTheDocument();
  });

  it('opens the confirmation dialog from ?confirm=approve and acts only when it is confirmed', async () => {
    getDispatch.mockResolvedValue(FIXTURE);
    const user = userEvent.setup();
    renderAt('/dispatch/d3adbeef?confirm=approve');

    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(approveDispatch).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Approve' }));

    await waitFor(() => expect(approveDispatch).toHaveBeenCalledWith('d3adbeef', ''));
  });

  it('opens the confirmation dialog from ?confirm=reject', async () => {
    getDispatch.mockResolvedValue(FIXTURE);
    const user = userEvent.setup();
    renderAt('/dispatch/d3adbeef?confirm=reject');

    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(rejectDispatch).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Reject' }));

    await waitFor(() => expect(rejectDispatch).toHaveBeenCalledWith('d3adbeef', ''));
  });

  it.each(['approve', 'reject'])(
    'shows nothing actionable for ?confirm=%s on a dispatch that is not waiting',
    async (decision) => {
      getDispatch.mockResolvedValue({ ...FIXTURE, state: 'MONITORING', gates: { lines: {} } });
      renderAt(`/dispatch/d3adbeef?confirm=${decision}`);

      await screen.findByRole('heading', { name: 'd3adbeef' });
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'approve d3adbeef' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'reject d3adbeef' })).not.toBeInTheDocument();
      expect(screen.getByText('This dispatch is not waiting for a person.')).toBeInTheDocument();
      expect(approveDispatch).not.toHaveBeenCalled();
      expect(rejectDispatch).not.toHaveBeenCalled();
    },
  );

  it('drops an ignored ?confirm= so the dialog never opens by itself later', async () => {
    getDispatch.mockResolvedValue({ ...FIXTURE, state: 'MONITORING', gates: { lines: {} } });
    renderAt('/dispatch/d3adbeef?confirm=approve&tab=lines');

    await screen.findByRole('heading', { name: 'd3adbeef' });
    await waitFor(() => expect(screen.getByTestId('search')).toHaveTextContent('?tab=lines'));
    expect(screen.getByTestId('search')).not.toHaveTextContent('confirm');
  });

  it('does not remember an id the backend does not serve', async () => {
    rememberDispatch('d3adbeef');
    getDispatch.mockResolvedValue(null);
    renderAt('/dispatch/gone-one');

    expect(await screen.findByText('Not found')).toBeInTheDocument();
    expect(readDispatchHistory()).toEqual(['d3adbeef']);
  });

  it('forgets a remembered id once the backend stops serving it', async () => {
    rememberDispatch('d3adbeef');
    rememberDispatch('gone-one');
    getDispatch.mockResolvedValue(null);
    renderAt('/dispatch/gone-one');

    expect(await screen.findByText('Not found')).toBeInTheDocument();
    await waitFor(() => expect(readDispatchHistory()).toEqual(['d3adbeef']));
  });

  it('says a reassign was queued, on the line it was queued for', async () => {
    // A drive owns the dispatch: the backend queues the reassign and the
    // dispatch it answers with (and reads back) names the queued request.
    const queued = {
      ...TWO_LINE_FIXTURE,
      human_reassigns: {
        queued: [{ line_id: 'line-0', kind: 'reassign', model: 'gpt-5.6-terra' }],
        dropped: [],
      },
    };
    getDispatch.mockResolvedValue(TWO_LINE_FIXTURE);
    reassignDispatchLine.mockImplementation(async () => {
      getDispatch.mockResolvedValue(queued);
      return queued;
    });
    const user = userEvent.setup();
    renderAt('/dispatch/d3adbeef');

    const field = await screen.findByLabelText('Model line-0');
    await user.type(field, 'gpt-5.6-terra');
    await user.click(within(field.closest('form') as HTMLElement).getByRole('button', { name: 'Reassign' }));

    await waitFor(() =>
      expect(screen.getByLabelText('Model line-0').closest('form')).toHaveTextContent(
        'Reassign to gpt-5.6-terra is queued',
      ),
    );
    expect(screen.getByLabelText('Model line-1').closest('form')).not.toHaveTextContent('queued');
  });

  it('names the provider of a reviewer the catalog only knows by prefix', async () => {
    getProviderCatalog.mockResolvedValue({ byModel: {}, providers: ['kimi', 'codex'] });
    getDispatch.mockResolvedValue({
      ...FIXTURE,
      gates: {
        lines: {
          'line-0': {
            ...FIXTURE.gates.lines['line-0'],
            gates: { primary_review: { result: 'pass', verdict: 'merge', reviewer: 'kimi-k3-review' } },
          },
        },
      },
    });
    renderAt('/dispatch/d3adbeef');

    const table = await screen.findByRole('table');
    await waitFor(() =>
      expect(within(table).getByText('kimi-k3-review').closest('td')).not.toHaveTextContent(
        'unknown provider',
      ),
    );
    expect(within(table).getByText('kimi-k3-review').closest('td')).toHaveTextContent('kimi');
  });

  it('keeps the note and reports a conflict when the dispatch moved on', async () => {
    getDispatch.mockResolvedValue(FIXTURE);
    approveDispatch.mockRejectedValue({ status: 409 });
    const user = userEvent.setup();
    renderAt('/dispatch/d3adbeef');

    await screen.findByRole('button', { name: 'approve d3adbeef' });
    await user.type(screen.getByLabelText('Note'), 'keep me');
    await user.click(screen.getByRole('button', { name: 'approve d3adbeef' }));
    await user.click(screen.getByRole('button', { name: 'Approve' }));

    expect(await screen.findByText('The dispatch changed state.')).toBeInTheDocument();
    expect(approveDispatch).toHaveBeenCalledWith('d3adbeef', 'keep me');
  });

  it('reassigns one line to another model', async () => {
    getDispatch.mockResolvedValue(FIXTURE);
    const user = userEvent.setup();
    renderAt('/dispatch/d3adbeef');

    const field = await screen.findByLabelText('Model line-0');
    await user.type(field, 'gpt-5.6-terra');
    await user.click(screen.getByRole('button', { name: 'Reassign' }));

    await waitFor(() =>
      expect(reassignDispatchLine).toHaveBeenCalledWith('d3adbeef', 'line-0', 'gpt-5.6-terra'),
    );
  });

  it('keeps a reassign refusal and the model on the line it belongs to', async () => {
    getDispatch.mockResolvedValue(TWO_LINE_FIXTURE);
    reassignDispatchLine.mockRejectedValue({
      response: { status: 409, data: { detail: 'reassign_pending' } },
    });
    const user = userEvent.setup();
    renderAt('/dispatch/d3adbeef');

    const field = await screen.findByLabelText('Model line-0');
    await user.type(field, 'gpt-5.6-terra');
    await user.click(within(field.closest('form') as HTMLElement).getByRole('button', { name: 'Reassign' }));

    expect(await screen.findByText('reassign_pending')).toBeInTheDocument();
    // The refusal is on the line that was refused, and the model the
    // operator typed is still there to correct.
    expect(field.closest('form')).toHaveTextContent('reassign_pending');
    expect(screen.getByLabelText('Model line-1').closest('form')).not.toHaveTextContent(
      'reassign_pending',
    );
    expect(screen.getByLabelText('Model line-0')).toHaveValue('gpt-5.6-terra');
  });

  it('keeps the line verdict pending until the secondary review passes', async () => {
    // The backend records "approve" for a primary review of a risk bearing
    // line: the primary passed, the secondary review is still to come.
    const record = {
      phase: 'secondary_review',
      risk_bearing: true,
      history: [{ gate: 'primary_review', result: 'approve' }],
    };
    expect(lineVerdict(record).id).toBe('pending');

    getDispatch.mockResolvedValue({
      ...FIXTURE,
      state: 'REVIEW',
      gates: { lines: { 'line-0': record } },
    });
    renderAt('/dispatch/d3adbeef');

    expect(await screen.findByText('L1')).toBeInTheDocument();
    const card = screen.getByText('L1').closest('article');
    expect(card).toHaveTextContent('pending');
    expect(card).not.toHaveTextContent('merge');
  });

  it('renders every review round the backend kept', () => {
    const rows = verdictRows({
      id: 'd',
      state: 'REVIEW',
      lines: [{ id: 'line-0' }],
      gates: {
        lines: {
          'line-0': {
            phase: 'fixing',
            primary_reviewer: 'codex',
            gates: { primary_review: { result: 'fail', reviewer: 'codex' } },
            history: [
              { round: 0, gate: 'primary_review', result: 'block', reviewer: 'codex', blocker_count: 2 },
              { round: 1, gate: 'primary_review', result: 'block', reviewer: 'codex', blocker_count: 1 },
            ],
          },
        },
      },
    });

    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({
      lineId: 'line-0',
      gate: 'primary_review',
      reviewer: 'codex',
      round: 0,
      blockerCount: 2,
    });
    expect(rows[1]).toMatchObject({ round: 1, blockerCount: 1 });
    expect(rows[0].verdict.id).toBe('block');
  });

  it('shows the reviewed revision, not the branch base', async () => {
    getDispatch.mockResolvedValue({
      ...FIXTURE,
      lines: [{ ...FIXTURE.lines[0], base_commit: 'aaaaaaaa' }],
      gates: {
        lines: {
          'line-0': {
            ...FIXTURE.gates.lines['line-0'],
            last_review_blockers: { head: 'bbbbbbbb', branch: 'line/line-0' },
          },
        },
        approval: { token_present: true },
      },
    });
    renderAt('/dispatch/d3adbeef');

    expect(await screen.findByText('bbbbbbbb')).toBeInTheDocument();
    expect(screen.queryByText('aaaaaaaa')).not.toBeInTheDocument();
  });

  it('says the reviewed revision is unknown when the backend recorded none', async () => {
    getDispatch.mockResolvedValue({
      ...FIXTURE,
      lines: [{ ...FIXTURE.lines[0], base_commit: 'aaaaaaaa' }],
    });
    renderAt('/dispatch/d3adbeef');

    expect(await screen.findByText('L1')).toBeInTheDocument();
    expect(screen.queryByText('aaaaaaaa')).not.toBeInTheDocument();
    const row = screen.getByRole('cell', { name: /—/ });
    expect(row).toBeInTheDocument();
  });

  it('keeps the eight named positions and does not invent a pass', () => {
    const axis = timelineOf({
      phase: 'awaiting_human',
      gates: {
        G0: { result: 'pass', reason: 'planner_output_valid' },
        G6: { result: 'pending', reason: 'awaiting_human' },
        primary_review: { result: 'pass', verdict: 'merge' },
      },
      events: [{ gate: 'G2', result: 'pass', reason: 'focused_tests_passed' }],
    });
    expect(axis.map((slot) => slot.id)).toEqual([
      'g0', 'd1', 'implementing', 'g2', 'primary', 'secondary', 'g5', 'g6',
    ]);
    expect(axis.find((slot) => slot.id === 'g6')?.status).toBe('pending-approval');
    expect(axis.find((slot) => slot.id === 'g6')?.current).toBe(true);
    expect(axis.find((slot) => slot.id === 'g2')?.status).toBe('pass');
    expect(axis.filter((slot) => slot.status === 'unreached').map((slot) => slot.id)).toEqual([
      'd1', 'implementing', 'secondary', 'g5',
    ]);
    expect(axis.some((slot) => slot.id === 'g1' || slot.id === 'g3' || slot.id === 'g4')).toBe(false);
    expect(verdictOf({ gates: { primary_review: { verdict: 'block' } } })).toBe('BLOCK');
  });

  it('places every known phase and leaves the other positions unreached', () => {
    const phases: Array<[string, string]> = [
      ['planning', 'g0'],
      ['spec_review', 'g0'],
      ['dag_ready', 'd1'],
      ['assigned', 'implementing'],
      ['implementing', 'implementing'],
      ['focused_tests', 'g2'],
      ['primary_review', 'primary'],
      ['secondary_review', 'secondary'],
      ['integration_checks', 'g5'],
      ['awaiting_human', 'g6'],
    ];
    for (const [phase, id] of phases) {
      const axis = timelineOf({ phase });
      expect(axis).toHaveLength(8);
      expect(axis.find((slot) => slot.id === id)?.current).toBe(true);
      expect(axis.filter((slot) => slot.id !== id).every((slot) => slot.status === 'unreached')).toBe(true);
    }
    const fixing = timelineOf({ phase: 'fixing', return_stage: 'primary_review' });
    expect(fixing.find((slot) => slot.id === 'primary')).toMatchObject({ status: 'fixing', current: true });
    expect(timelineOf({ phase: 'merged' }).every((slot) => slot.status === 'merged')).toBe(true);
    const blocked = timelineOf({
      phase: 'blocked',
      machine: { stage: 'blocked', return_stage: 'primary_review' },
      events: [{ from_stage: 'integration_checks', to_stage: 'blocked', gate: 'G5' }],
    });
    expect(blocked.find((slot) => slot.id === 'g5')).toMatchObject({ status: 'blocked', current: true });
    expect(blocked.find((slot) => slot.id === 'primary')?.current).toBe(false);
    expect(blocked.find((slot) => slot.id === 'g6')?.status).toBe('unreached');
    const quiet = timelineOf({ phase: 'implementing', risk_bearing: false });
    expect(quiet.find((slot) => slot.id === 'secondary')?.status).toBe('not-applicable');
    expect(timelineOf({ phase: 'G1' }).every((slot) => slot.status === 'unreached')).toBe(true);
  });

  it('shows the W0 review trail on the named axis', () => {
    const fixture = w0Fixture();
    const axis = timelineOf(fixture.gates.lines['line-0']);
    const primary = axis.find((slot) => slot.id === 'primary');
    expect(axis.find((slot) => slot.id === 'g6')).toMatchObject({ status: 'pending-approval', current: true });
    expect(primary?.notes).toEqual(['review_blocked', 'fix_resubmitted', 'primary_review_passed']);
    expect(axis.find((slot) => slot.id === 'secondary')?.status).toBe('pass');
    expect(axis.map((slot) => slot.id).join(' ')).not.toMatch(/g1|g3|g4/);
  });

  it('renders the W0 fixture without naming G1, G3 or G4 and without the token', async () => {
    const fixture = w0Fixture();
    getDispatch.mockResolvedValue(fixture);
    renderAt('/dispatch/d3adbeef');

    expect(await screen.findAllByText('G6 human')).not.toHaveLength(0);
    expect(document.body.textContent).not.toMatch(/G1|G3|G4/);
    expect(screen.queryByText('tok-1')).not.toBeInTheDocument();
    expect(screen.queryByText('primary_verdict')).not.toBeInTheDocument();
  });

  it.each(['approve', 'reject'] as const)(
    'closes the %s dialog when the dispatch leaves APPROVAL',
    async (action) => {
      vi.useFakeTimers();
      try {
        let state = 'APPROVAL';
        getDispatch.mockImplementation(async () => ({ ...FIXTURE, state }));
        renderAt('/dispatch/d3adbeef');
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
        });
        fireEvent.click(
          screen.getByRole('button', { name: action === 'approve' ? 'approve d3adbeef' : 'reject d3adbeef' }),
        );
        expect(screen.getByRole('dialog')).toBeInTheDocument();
        state = 'COMPLETE';
        await act(async () => {
          await vi.advanceTimersByTimeAsync(5_000);
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(1);
        });
        expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
        expect(approveDispatch).not.toHaveBeenCalled();
        expect(rejectDispatch).not.toHaveBeenCalled();
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it.each(['COMPLETE', 'ABORT', 'ERROR'] as const)(
    'does not refetch a %s dispatch when the tab returns',
    async (state) => {
      getDispatch.mockResolvedValue({ ...FIXTURE, state });
      const spy = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
      renderAt('/dispatch/d3adbeef');
      await screen.findByRole('heading', { name: 'd3adbeef' });
      const calls = getDispatch.mock.calls.length;
      spy.mockReturnValue('hidden');
      document.dispatchEvent(new Event('visibilitychange'));
      spy.mockReturnValue('visible');
      document.dispatchEvent(new Event('visibilitychange'));
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 30));
      });
      expect(getDispatch.mock.calls.length).toBe(calls);
      spy.mockRestore();
    },
  );

  it('remembers a dispatch the user opened, and forgets it on request', () => {
    rememberDispatch('d3adbeef');
    expect(getDispatch).not.toHaveBeenCalled();
    forgetDispatch('d3adbeef');
    expect(getDispatch).not.toHaveBeenCalled();
  });
});
