// @vitest-environment node
import { describe, it, expect } from 'vitest';
import enUS from '@/locales/en-US.json';
import { assertNoNamespaceCollision, assertParity } from '@/lib/featureLocales';
import en from '../locales/tnSettings.en-US.json';
import zh from '../locales/tnSettings.zh-CN.json';

describe('tnSettings locales', () => {
  it('keeps en-US / zh-CN key parity', () => {
    expect(assertParity(en, zh)).toEqual({ enOnly: [], zhOnly: [] });
  });

  it('tnSettings does not collide with the main catalog', () => {
    expect(() =>
      assertNoNamespaceCollision('tnSettings', enUS as Record<string, unknown>),
    ).not.toThrow();
    expect(Object.keys(en)).toEqual(['tnSettings']);
    expect(Object.keys(zh)).toEqual(['tnSettings']);
  });

  it('keeps the {{section}} placeholder in both languages', () => {
    expect(en.tnSettings.eyebrow.section).toContain('{{section}}');
    expect(zh.tnSettings.eyebrow.section).toContain('{{section}}');
  });
});
