// @vitest-environment node
import { describe, it, expect } from 'vitest';
import enUS from '@/locales/en-US.json';
import { assertNoNamespaceCollision, assertParity } from '@/lib/featureLocales';
import en from '../locales/tnTicket.en-US.json';
import zh from '../locales/tnTicket.zh-CN.json';

describe('tnTicket locales', () => {
  it('keeps en-US / zh-CN key parity', () => {
    expect(assertParity(en, zh)).toEqual({ enOnly: [], zhOnly: [] });
  });

  it('tnTicket does not collide with the main catalog', () => {
    expect(() =>
      assertNoNamespaceCollision('tnTicket', enUS as Record<string, unknown>),
    ).not.toThrow();
    expect(Object.keys(en)).toEqual(['tnTicket']);
    expect(Object.keys(zh)).toEqual(['tnTicket']);
  });

  it('keeps the same interpolation slots in both languages', () => {
    const slots = (value: unknown, prefix = ''): string[] => {
      if (typeof value === 'string') {
        return [...value.matchAll(/\{\{(\w+)\}\}/g)].map((m) => `${prefix}:${m[1]}`).sort();
      }
      if (value && typeof value === 'object') {
        return Object.entries(value).flatMap(([k, v]) => slots(v, prefix ? `${prefix}.${k}` : k));
      }
      return [];
    };
    expect(slots(zh)).toEqual(slots(en));
  });
});
