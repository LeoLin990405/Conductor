// @vitest-environment node
import { describe, expect, it } from 'vitest';
import enUS from '@/locales/en-US.json';
import zhCN from '@/locales/zh-CN.json';
import { assertNoNamespaceCollision, assertParity } from '@/lib/featureLocales';
import en from '../locales/tnAgentFleet.en-US.json';
import zh from '../locales/tnAgentFleet.zh-CN.json';

const PROVIDERS = ['codex', 'kimi', 'cursor', 'grok', 'minimax', 'deepseek-dsh', 'zcode', 'stepfun'];

function values(node: unknown): string[] {
  if (typeof node === 'string') return [node];
  if (node && typeof node === 'object') return Object.values(node).flatMap(values);
  return [];
}

describe('tnAgentFleet locales', () => {
  it('keeps en-US / zh-CN key parity', () => {
    expect(assertParity(en, zh)).toEqual({ enOnly: [], zhOnly: [] });
  });

  it('tnAgentFleet is the only namespace and collides with no main-catalog namespace', () => {
    expect(Object.keys(en)).toEqual(['tnAgentFleet']);
    expect(Object.keys(zh)).toEqual(['tnAgentFleet']);
    expect(() =>
      assertNoNamespaceCollision('tnAgentFleet', enUS as Record<string, unknown>),
    ).not.toThrow();
    expect(() =>
      assertNoNamespaceCollision('tnAgentFleet', zhCN as Record<string, unknown>),
    ).not.toThrow();
  });

  it('names no provider in the copy: providers are data', () => {
    for (const text of [...values(en), ...values(zh)]) {
      for (const provider of PROVIDERS) {
        expect(text.toLowerCase(), text).not.toContain(provider);
      }
    }
  });

  it('keeps the interpolation placeholders the page passes in both languages', () => {
    const placeholders = (text: string) => (text.match(/\{\{\w+\}\}/g) ?? []).sort();
    const walk = (left: unknown, right: unknown, path: string) => {
      if (typeof left === 'string') {
        expect(placeholders(right as string), path).toEqual(placeholders(left));
        return;
      }
      for (const key of Object.keys(left as object)) {
        walk((left as Record<string, unknown>)[key], (right as Record<string, unknown>)[key], `${path}.${key}`);
      }
    };
    walk(en, zh, '');
  });
});
