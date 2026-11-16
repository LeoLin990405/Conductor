// @vitest-environment node
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { completeArgs, prefixMatch, uniquePrefixInsert } from '../argComplete';

const listActiveDispatches = vi.fn();

vi.mock('@/api/dispatch', () => ({
  listActiveDispatches: (...args: unknown[]) => listActiveDispatches(...args),
}));

const ROSTER = [
  { id: '7f3a91c2', phase: 'awaiting_human' },
  { id: '4b8e0d17', phase: 'secondary_review' },
  { id: '91cc6a40', phase: 'g2' },
  { id: '7c77be05', phase: 'fixing' },
];

describe('D2 argComplete', () => {
  beforeEach(() => {
    listActiveDispatches.mockReset();
    listActiveDispatches.mockResolvedValue(ROSTER);
  });

  it('dispatch lists all active ids', async () => {
    const result = await completeArgs('dispatch', '', listActiveDispatches);
    expect(result.options.map((o) => o.id)).toEqual([
      '7f3a91c2',
      '4b8e0d17',
      '91cc6a40',
      '7c77be05',
    ]);
  });

  it('approve / reject list only awaiting_human ids', async () => {
    const approve = await completeArgs('approve', '', listActiveDispatches);
    const reject = await completeArgs('reject', '', listActiveDispatches);
    expect(approve.options.map((o) => o.id)).toEqual(['7f3a91c2']);
    expect(reject.options.map((o) => o.id)).toEqual(['7f3a91c2']);
  });

  it('unique case-sensitive hex prefix inserts; ambiguous lists and inserts nothing', async () => {
    const all = await completeArgs('dispatch', '', listActiveDispatches);
    expect(uniquePrefixInsert('91', all.options)).toBe('91cc6a40');
    expect(uniquePrefixInsert('7', all.options)).toBeNull();
    expect(prefixMatch('7', all.options).map((o) => o.id)).toEqual(['7f3a91c2', '7c77be05']);
    expect(prefixMatch('DSP-9', all.options)).toHaveLength(0);
    expect(uniquePrefixInsert('DSP-9', all.options)).toBeNull();
  });

  it('reply offers nothing', async () => {
    const result = await completeArgs('reply', '', listActiveDispatches);
    expect(result.options).toEqual([]);
    expect(result.freeText).toBe(true);
    expect(listActiveDispatches).not.toHaveBeenCalled();
  });
});
