import { describe, expect, it } from 'vitest';
import type { FleetAgent } from '@/api/agentFleet';
import {
  AGENT_STATUSES,
  BALANCE_MARKS,
  EMPTY_FILTERS,
  STATUS_MARKS,
  providerRoster,
  filterAgents,
  formatElapsed,
  refreshIntervalFromQuery,
  sortAgents,
  summarizeTail,
} from '../fleetView';

function agent(overrides: Partial<FleetAgent>): FleetAgent {
  return {
    id: 'agent-fleet',
    model: 'codex',
    provider: 'codex',
    role: 'dev',
    pid: 101,
    worktree: '/Users/you/conductor-wt/agent-fleet',
    branch: 'feat/agent-fleet',
    status: 'running',
    elapsed_secs: 3723,
    last_activity_ts: '2026-11-02T12:00:00Z',
    log_file: 'agent-fleet.log',
    log_tail: 'starting work',
    commits_ahead: 3,
    tests_state: '12 passed',
    provider_balance: 'ok',
    ...overrides,
  };
}

const agents = [
  agent({ id: 'alpha', status: 'running', elapsed_secs: 60, commits_ahead: 5 }),
  agent({
    id: 'beta',
    status: 'stalled',
    elapsed_secs: null,
    commits_ahead: null,
    role: 'review',
    model: 'GLM-5.3',
    provider: 'zcode',
    tests_state: null,
  }),
  agent({ id: 'gamma', status: 'done', pid: null, elapsed_secs: 90_000, commits_ahead: 1 }),
];

describe('formatElapsed', () => {
  it('renders minutes, hours and days', () => {
    expect(formatElapsed(0)).toBe('00:00');
    expect(formatElapsed(65)).toBe('01:05');
    expect(formatElapsed(3723)).toBe('1:02:03');
    expect(formatElapsed(90_000)).toBe('1d 01h');
  });

  it('renders an unknown duration as a dash', () => {
    expect(formatElapsed(null)).toBe('—');
  });
});

describe('filterAgents', () => {
  it('keeps every agent with the empty filters', () => {
    expect(filterAgents(agents, EMPTY_FILTERS)).toHaveLength(3);
  });

  it('filters by status and role', () => {
    expect(filterAgents(agents, { ...EMPTY_FILTERS, status: 'stalled' }).map((a) => a.id)).toEqual(['beta']);
    expect(filterAgents(agents, { ...EMPTY_FILTERS, role: 'review' }).map((a) => a.id)).toEqual(['beta']);
  });

  it('filters provider and model case-insensitively by substring', () => {
    expect(filterAgents(agents, { ...EMPTY_FILTERS, provider: 'ZC' }).map((a) => a.id)).toEqual(['beta']);
    expect(filterAgents(agents, { ...EMPTY_FILTERS, model: 'glm' }).map((a) => a.id)).toEqual(['beta']);
    expect(filterAgents(agents, { ...EMPTY_FILTERS, model: 'nope' })).toEqual([]);
  });
});

describe('sortAgents', () => {
  it('sorts by elapsed with unknown last', () => {
    expect(sortAgents(agents, 'elapsed').map((a) => a.id)).toEqual(['gamma', 'alpha', 'beta']);
  });

  it('sorts by commits with unknown last', () => {
    expect(sortAgents(agents, 'commits').map((a) => a.id)).toEqual(['alpha', 'gamma', 'beta']);
  });

  it('sorts by status severity, live work first', () => {
    expect(sortAgents(agents, 'status').map((a) => a.id)).toEqual(['alpha', 'beta', 'gamma']);
  });

  it('sorts by id, role and model', () => {
    expect(sortAgents(agents, 'id').map((a) => a.id)).toEqual(['alpha', 'beta', 'gamma']);
    expect(sortAgents(agents, 'role').map((a) => a.id)).toEqual(['alpha', 'gamma', 'beta']);
    // Collation is locale-aware, so 'codex' sorts before 'GLM-5.3'.
    expect(sortAgents(agents, 'model').map((a) => a.id)).toEqual(['alpha', 'gamma', 'beta']);
  });

  it('does not mutate the input', () => {
    const before = agents.map((a) => a.id);
    sortAgents(agents, 'elapsed');
    expect(agents.map((a) => a.id)).toEqual(before);
  });
});

describe('summarizeTail', () => {
  it('flattens whitespace and truncates', () => {
    expect(summarizeTail('line one\n  line two')).toBe('line one line two');
    expect(summarizeTail('x'.repeat(200), 20)).toHaveLength(20);
    expect(summarizeTail('')).toBe('—');
  });
});

describe('refreshIntervalFromQuery', () => {
  it('defaults to 30 seconds', () => {
    expect(refreshIntervalFromQuery('')).toBe(30_000);
  });

  it('reads and clamps the refresh parameter', () => {
    expect(refreshIntervalFromQuery('refresh=10')).toBe(10_000);
    expect(refreshIntervalFromQuery('refresh=0')).toBe(0);
    expect(refreshIntervalFromQuery('refresh=99999')).toBe(600_000);
    expect(refreshIntervalFromQuery('refresh=abc')).toBe(30_000);
  });
});

describe('providerRoster', () => {
  it('groups agents by provider in name order with the worst balance', () => {
    const rows = providerRoster([
      agent({ id: 'a', provider: 'kimi', model: 'k3', status: 'running', provider_balance: 'ok' }),
      agent({ id: 'b', provider: 'codex', model: 'gpt', status: 'done', provider_balance: 'ok' }),
      agent({ id: 'c', provider: 'kimi', model: 'k2', status: 'stalled', provider_balance: 'unknown' }),
      agent({ id: 'd', provider: 'kimi', model: 'k3', status: 'running', provider_balance: 'ok' }),
      agent({ id: 'e', provider: 'codex', model: '', status: 'error', provider_balance: 'low' }),
    ]);
    expect(rows).toEqual([
      { provider: 'codex', models: ['gpt'], agents: 2, running: 0, balance: 'low' },
      { provider: 'kimi', models: ['k2', 'k3'], agents: 3, running: 2, balance: 'unknown' },
    ]);
  });

  it('is empty for an empty scan', () => {
    expect(providerRoster([])).toEqual([]);
  });
});

describe('state marks', () => {
  it('give every status and balance a glyph from the state grammar', () => {
    const grammar = ['○', '●', '▣', '◆', '✖', '✔', '⊘'];
    for (const mark of [...Object.values(STATUS_MARKS), ...Object.values(BALANCE_MARKS)]) {
      expect(grammar).toContain(mark.glyph);
    }
    expect(Object.keys(STATUS_MARKS).sort()).toEqual([...AGENT_STATUSES].sort());
  });
});
