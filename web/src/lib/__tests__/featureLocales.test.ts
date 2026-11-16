import { describe, it, expect, beforeEach } from 'vitest';
import i18n from '@/i18n';
import enUS from '@/locales/en-US.json';
import {
  assertNoNamespaceCollision,
  assertParity,
  registerFeatureLocales,
} from '../featureLocales';

describe('featureLocales', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('en-US');
  });

  it('registerFeatureLocales merges without overwriting existing keys', () => {
    registerFeatureLocales({
      'en-US': { tnFeatureLocalesTest: { hello: 'world' } },
    });
    expect(i18n.t('tnFeatureLocalesTest.hello')).toBe('world');
    registerFeatureLocales({
      'en-US': { tnFeatureLocalesTest: { hello: 'replaced' } },
    });
    // overwrite=false keeps the first value
    expect(i18n.t('tnFeatureLocalesTest.hello')).toBe('world');
  });

  it('assertParity passes on matching trees and fails on drift', () => {
    const en = { tnX: { a: '1', b: { c: '2' } } };
    const zh = { tnX: { a: '一', b: { c: '二' } } };
    expect(assertParity(en, zh)).toEqual({ enOnly: [], zhOnly: [] });
    expect(() => assertParity(en, { tnX: { a: '一' } })).toThrow(/parity/);
  });

  it('assertNoNamespaceCollision rejects a main-catalog namespace', () => {
    expect(() => assertNoNamespaceCollision('settings', enUS as Record<string, unknown>)).toThrow(
      /collides/,
    );
    expect(() =>
      assertNoNamespaceCollision('tnPalette', enUS as Record<string, unknown>),
    ).not.toThrow();
  });
});
