// @vitest-environment node
import { describe, it, expect } from 'vitest';
import enUS from '@/locales/en-US.json';
import { assertNoNamespaceCollision, assertParity } from '@/lib/featureLocales';
import en from '../locales/tnPalette.en-US.json';
import zh from '../locales/tnPalette.zh-CN.json';

describe('tnPalette locales', () => {
  it('keeps en-US / zh-CN key parity', () => {
    expect(assertParity(en, zh)).toEqual({ enOnly: [], zhOnly: [] });
  });

  it('tnPalette does not collide with the main catalog', () => {
    expect(() =>
      assertNoNamespaceCollision('tnPalette', enUS as Record<string, unknown>),
    ).not.toThrow();
    expect(Object.keys(en)).toEqual(['tnPalette']);
    expect(Object.keys(zh)).toEqual(['tnPalette']);
  });
});
