// @vitest-environment node
import { describe, it, expect } from 'vitest';
import enUS from '@/locales/en-US.json';
import { assertNoNamespaceCollision, assertParity } from '@/lib/featureLocales';
import en from '../locales/tnDispatch.en-US.json';
import zh from '../locales/tnDispatch.zh-CN.json';

describe('tnDispatch locales', () => {
  it('keeps en-US / zh-CN key parity', () => {
    expect(assertParity(en, zh)).toEqual({ enOnly: [], zhOnly: [] });
  });

  it('tnDispatch does not collide with the main catalog', () => {
    expect(() =>
      assertNoNamespaceCollision('tnDispatch', enUS as Record<string, unknown>),
    ).not.toThrow();
    expect(Object.keys(en)).toEqual(['tnDispatch']);
    expect(Object.keys(zh)).toEqual(['tnDispatch']);
  });

  // The planner caps a plan at 8 lines (planning.py max_lines=8); 4 is only the
  // default CONDUCTOR_DISPATCH_MAX_PARALLEL_LINES, not a cap on lines per dispatch.
  it('lede states the 8-line plan cap and the 4-line parallel default', () => {
    expect(en.tnDispatch.lede).toContain('up to eight lines');
    expect(en.tnDispatch.lede).toContain('up to four at once');
    expect(en.tnDispatch.lede).not.toContain('owns up to four lines');
    expect(zh.tnDispatch.lede).toContain('最多八条线');
    expect(zh.tnDispatch.lede).toContain('最多四条同时运行');
    expect(zh.tnDispatch.lede).not.toContain('一个派发最多四条线');
  });
});
