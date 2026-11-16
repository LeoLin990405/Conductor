import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { timelineOf, type GateRecord } from '../model';

interface Vector {
  id: string;
  current: string | null;
  record: GateRecord;
  slots: Array<{ id: string; status: string; current: boolean }>;
  notes?: Record<string, string[]>;
}

function vectors(): Vector[] {
  const candidates = [
    resolve(process.cwd(), '../tests/fixtures/dispatch_timeline_vectors_v1.json'),
    resolve(process.cwd(), 'tests/fixtures/dispatch_timeline_vectors_v1.json'),
  ];
  const found = candidates.find((path) => existsSync(path));
  if (!found) throw new Error(`timeline vectors not found from ${process.cwd()}`);
  return JSON.parse(readFileSync(found, 'utf8')).vectors;
}

describe('shared timeline vectors', () => {
  it.each(vectors())('$id', (vector) => {
    const slots = timelineOf(vector.record);
    const current = slots.find((slot) => slot.current)?.id ?? null;
    expect(current).toBe(vector.current);
    expect(slots.map((slot) => slot.id)).toEqual(['g0', 'd1', 'implementing', 'g2', 'primary', 'secondary', 'g5', 'g6']);
    for (const expected of vector.slots) {
      const got = slots.find((slot) => slot.id === expected.id);
      expect(got?.status, `${vector.id} ${expected.id}`).toBe(expected.status);
      expect(got?.current, `${vector.id} ${expected.id}`).toBe(expected.current);
    }
    for (const [slotId, notes] of Object.entries(vector.notes ?? {})) {
      expect(slots.find((slot) => slot.id === slotId)?.notes).toEqual(notes);
    }
  });
});
