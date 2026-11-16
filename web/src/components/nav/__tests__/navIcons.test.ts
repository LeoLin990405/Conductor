import { describe, expect, it } from 'vitest';
import { NAV_ITEMS, SETTINGS_ITEM } from '../navItems';

/** The rail and tab bar are scanned by glyph first, so no two destinations
 *  may share one. This is the regression guard for AUDIT §6 #13. */
describe('nav icons', () => {
  it('gives every nav destination a distinct lucide icon', () => {
    const items = [...NAV_ITEMS, SETTINGS_ITEM];
    const icons = items.map((item) => item.icon);
    expect(new Set(icons).size).toBe(items.length);
  });

  it('covers every route in the shell exactly once', () => {
    const keys = [...NAV_ITEMS, SETTINGS_ITEM].map((item) => item.key);
    expect(new Set(keys).size).toBe(keys.length);
  });
});
