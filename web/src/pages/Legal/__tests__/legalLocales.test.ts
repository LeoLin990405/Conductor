// @vitest-environment node
import { describe, it, expect } from 'vitest';
import enUS from '@/locales/en-US.json';
import { assertNoNamespaceCollision, assertParity } from '@/lib/featureLocales';
import en from '../locales/tnLegal.en-US.json';
import zh from '../locales/tnLegal.zh-CN.json';

describe('tnLegal locales', () => {
  it('keeps en-US / zh-CN key parity', () => {
    expect(assertParity(en, zh)).toEqual({ enOnly: [], zhOnly: [] });
  });

  it('tnLegal does not collide with the main catalog', () => {
    expect(() =>
      assertNoNamespaceCollision('tnLegal', enUS as Record<string, unknown>),
    ).not.toThrow();
    expect(Object.keys(en)).toEqual(['tnLegal']);
    expect(Object.keys(zh)).toEqual(['tnLegal']);
  });

  it('both languages keep the same inline link tags', () => {
    const tags = (s: string) => [...s.matchAll(/<(\w+)>/g)].map((m) => m[1]).sort();
    for (const key of Object.keys(en.tnLegal) as Array<keyof typeof en.tnLegal>) {
      expect(tags(zh.tnLegal[key]), key).toEqual(tags(en.tnLegal[key]));
    }
  });
});
