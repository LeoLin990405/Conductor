// @vitest-environment node
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, it, expect } from 'vitest';
import { COMMANDS, COMPLETION_LIMIT, IDLE_ORDER } from '../commands';
import { fuzzyMatch, rankCommands } from '../matcher';

const tokens = JSON.parse(
  readFileSync(
    resolve(__dirname, '../../../../../docs/design/terminal-native/system/tokens.json'),
    'utf8',
  ),
) as {
  palette: {
    fuzzy: {
      examples: Array<{
        query: string;
        match: string | null;
        haystack?: string;
        gapCost?: number;
        indexes?: number[];
      }>;
    };
    idleOrder: string[];
    completionLimit: number;
  };
};

describe('D1 matcher', () => {
  it('iterates palette.fuzzy.examples from tokens.json', () => {
    for (const ex of tokens.palette.fuzzy.examples) {
      const rows = rankCommands(ex.query);
      if (ex.match === null) {
        expect(rows).toHaveLength(0);
        continue;
      }
      expect(rows[0]?.command.name).toBe(ex.match);
      if (ex.indexes && ex.gapCost !== undefined) {
        const score = fuzzyMatch(ex.query, ex.haystack ?? ex.match);
        expect(score).not.toBeNull();
        expect(score!.idx).toEqual(ex.indexes);
        expect(score!.gap).toBe(ex.gapCost);
      }
    }
  });

  it('covers the D1 list: dsp/DSP/jobs/Disp/tkt/apv/zzz and idleOrder', () => {
    expect(rankCommands('dsp')[0]?.command.name).toBe('dispatch');
    expect(fuzzyMatch('dsp', 'dispatch')?.gap).toBe(1);
    expect(rankCommands('DSP')[0]?.command.name).toBe('dispatch');
    expect(rankCommands('jobs')[0]?.command.name).toBe('tickets');
    expect(rankCommands('jobs')[0]?.onName).toBe(false);
    expect(rankCommands('Disp')[0]?.command.name).toBe('dispatch');
    expect(rankCommands('tkt')[0]?.command.name).toBe('tickets');
    expect(rankCommands('apv')[0]?.command.name).toBe('approve');
    expect(rankCommands('zzz')).toHaveLength(0);

    const idle = rankCommands('');
    expect(idle).toHaveLength(IDLE_ORDER.length);
    expect(idle.map((r) => r.command.name)).toEqual([...IDLE_ORDER]);
    expect(idle).toHaveLength(28);
  });

  it('never returns more than 60 rows', () => {
    expect(COMPLETION_LIMIT).toBe(60);
    expect(tokens.palette.completionLimit).toBe(60);
    expect(rankCommands('').length).toBeLessThanOrEqual(COMPLETION_LIMIT);
    expect(rankCommands('a').length).toBeLessThanOrEqual(COMPLETION_LIMIT);
    expect(COMMANDS.length).toBeLessThanOrEqual(COMPLETION_LIMIT);
  });
});
