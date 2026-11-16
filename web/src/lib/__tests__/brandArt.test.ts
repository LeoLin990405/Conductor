/**
 * The configuration surfaces draw one logo per provider. These pin the marks
 * the manifest's providers and variants resolve to, and that an unknown/custom
 * provider still gets `undefined` (BrandMark then draws its monogram).
 */
import { describe, expect, it } from 'vitest';
import { llmProviderArt } from '@/lib/brandArt';

const EXPECTED = [
  'openai',
  'codex-oauth',
  'anthropic',
  'claude-oauth',
  'gemini',
  'gemini-oauth',
  'openrouter',
  'grok',
  'grok-oauth',
  'xai',
  'z-ai',
  'z-ai-coding',
  'z-ai-cn',
  'z-ai-cn-coding',
  'z-ai-oauth',
  'z-ai-cn-oauth',
  'minimax',
  'minimax-coding',
  'minimax-intl',
  'dashscope',
  'dashscope-intl',
  'volcengine',
  'doubao-coding',
  'doubao-plan',
  'moonshot',
  'moonshot-coding',
  'kimi-oauth',
  'deepseek',
  'deepseek-account',
  'mimo',
  'mimo-oauth',
  'stepfun',
  'stepfun-plan',
  'groq',
  'cerebras',
  'deepinfra',
  'ollama',
  'lm-studio',
  'vllm',
];

describe('provider marks', () => {
  it('resolves every provider and variant to bundled art', () => {
    for (const provider of EXPECTED) {
      expect(llmProviderArt(provider), provider).toBeDefined();
    }
  });

  it('leaves unknown, custom and auto providers to the monogram fallback', () => {
    expect(llmProviderArt('__auto__')).toBeUndefined();
    expect(llmProviderArt('my-custom-provider')).toBeUndefined();
  });
});
