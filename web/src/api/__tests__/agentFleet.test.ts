import { describe, expect, it } from 'vitest';
import { api } from '@/api/client';
import { getAgentFleet, getAgentLogs } from '../agentFleet';

vi.mock('@/api/client', () => ({
  api: {
    get: vi.fn(),
  },
}));

const get = api.get as unknown as ReturnType<typeof vi.fn>;

describe('getAgentFleet', () => {
  it('unwraps the fleet envelope', async () => {
    const payload = {
      agents: [],
      scan_ts: '2026-11-02T12:00:00Z',
      stall_threshold_mins: 5,
      error: null,
    };
    get.mockResolvedValue({ data: payload });

    await expect(getAgentFleet()).resolves.toEqual(payload);
    expect(get).toHaveBeenCalledWith('/api/v1/agents/fleet');
  });
});

describe('getAgentLogs', () => {
  it('encodes the agent id and bounds the line count', async () => {
    get.mockResolvedValue({ data: { agent_id: 'a/b', log_file: 'a-b.log', lines: [], truncated: false } });

    await getAgentLogs('a/b', 50);

    expect(get).toHaveBeenCalledWith('/api/v1/agents/a%2Fb/logs', { params: { lines: 50 } });
  });

  it('defaults to 100 lines', async () => {
    get.mockResolvedValue({ data: { agent_id: 'x', log_file: 'x.log', lines: [], truncated: false } });

    await getAgentLogs('x');

    expect(get).toHaveBeenCalledWith('/api/v1/agents/x/logs', { params: { lines: 100 } });
  });
});
