// @vitest-environment node
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import i18n from '@/i18n';
import { assertNoNamespaceCollision, assertParity } from '@/lib/featureLocales';
import { NAV_ITEMS, SETTINGS_ITEM } from '../../nav/navItems';
import { SHELL_LOCALES } from '../shellLocales';
import en from '../locales/tnShell.en-US.json';
import zh from '../locales/tnShell.zh-CN.json';

describe('tnShell locales', () => {
  it('keeps en-US / zh-CN key parity', () => {
    expect(assertParity(en, zh)).toEqual({ enOnly: [], zhOnly: [] });
  });

  it('tnShell does not collide with the main catalog', () => {
    // Read the catalog from disk, not the imported object: i18next's deep
    // addResourceBundle writes into the same translation object test/setup.ts
    // handed it, which would make the freshly registered tnShell look like a
    // pre-existing main-catalog namespace.
    const main = JSON.parse(
      readFileSync(new URL('../../../locales/en-US.json', import.meta.url), 'utf8'),
    ) as Record<string, unknown>;
    expect(() => assertNoNamespaceCollision('tnShell', main)).not.toThrow();
    expect(Object.keys(en)).toEqual(['tnShell']);
    expect(Object.keys(zh)).toEqual(['tnShell']);
  });

  it('registers both bundles at module load', () => {
    expect(SHELL_LOCALES['en-US']).toBe(en);
    expect(SHELL_LOCALES['zh-CN']).toBe(zh);
    expect(i18n.getResourceBundle('en-US', 'translation')).toHaveProperty('tnShell');
    expect(i18n.getResourceBundle('zh-CN', 'translation')).toHaveProperty('tnShell');
  });

  it('resolves every nav label and the palette trigger in both languages', () => {
    const keys = [
      ...NAV_ITEMS.map((item) => item.labelKey),
      SETTINGS_ITEM.labelKey,
      'tnShell.palette.open',
      'tnShell.palette.trigger',
    ];
    for (const key of keys) {
      expect(i18n.t(key, { lng: 'en-US' })).not.toBe(key);
      expect(i18n.t(key, { lng: 'zh-CN' })).not.toBe(key);
    }
  });
});
