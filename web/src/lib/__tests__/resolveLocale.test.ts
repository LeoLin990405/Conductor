// @vitest-environment node
import { describe, expect, it } from 'vitest';
import { DEFAULT_LOCALE, resolveLocale } from '../resolveLocale';

const locales = ['en-US', 'zh-CN'] as const;

describe('Conductor product locale', () => {
  it.each(['en-US', 'zh-CN', 'fr-FR', ''])('defaults to Chinese for browser %j', (browser) => {
    expect(DEFAULT_LOCALE).toBe('zh-CN');
    expect(resolveLocale('', browser, locales, DEFAULT_LOCALE)).toBe('zh-CN');
  });

  it('honors an explicit language choice while rejecting an unknown cookie', () => {
    expect(resolveLocale('locale=en-US', 'zh-CN', locales, DEFAULT_LOCALE)).toBe('en-US');
    expect(resolveLocale('locale=unknown', 'en-US', locales, DEFAULT_LOCALE)).toBe('zh-CN');
  });
});
