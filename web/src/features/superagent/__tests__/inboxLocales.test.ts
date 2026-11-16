// @vitest-environment node
import { describe, it, expect } from 'vitest';
import enUS from '@/locales/en-US.json';
import { assertNoNamespaceCollision, assertParity } from '@/lib/featureLocales';
import en from '../locales/tnInbox.en-US.json';
import zh from '../locales/tnInbox.zh-CN.json';

describe('tnInbox locales', () => {
  it('keeps en-US / zh-CN key parity', () => {
    expect(assertParity(en, zh)).toEqual({ enOnly: [], zhOnly: [] });
  });

  it('tnInbox does not collide with the main catalog', () => {
    expect(() =>
      assertNoNamespaceCollision('tnInbox', enUS as Record<string, unknown>),
    ).not.toThrow();
    expect(Object.keys(en)).toEqual(['tnInbox']);
    expect(Object.keys(zh)).toEqual(['tnInbox']);
  });
});
