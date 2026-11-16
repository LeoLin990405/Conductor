// @vitest-environment node
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, it, expect } from 'vitest';
import { COMMANDS, IDLE_ORDER } from '../commands';

const tokens = JSON.parse(
  readFileSync(
    resolve(__dirname, '../../../../../docs/design/terminal-native/system/tokens.json'),
    'utf8',
  ),
) as {
  palette: {
    commands: Array<{ name: string; slash: string | null; aliases: string[]; hint: string }>;
    idleOrder: string[];
  };
};

describe('commandsSync', () => {
  it('matches palette.commands / idleOrder in tokens.json', () => {
    expect(COMMANDS).toHaveLength(28);
    expect(IDLE_ORDER).toHaveLength(28);
    expect([...IDLE_ORDER]).toEqual(tokens.palette.idleOrder);

    expect(COMMANDS.map((c) => c.name)).toEqual(tokens.palette.commands.map((c) => c.name));
    for (let i = 0; i < COMMANDS.length; i++) {
      const got = COMMANDS[i]!;
      const want = tokens.palette.commands[i]!;
      expect(got.slash).toBe(want.slash);
      expect([...got.aliases]).toEqual(want.aliases ?? []);
      expect(got.hint).toBe(want.hint);
    }
  });
});
