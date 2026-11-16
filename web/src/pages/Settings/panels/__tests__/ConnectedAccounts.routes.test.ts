import { describe, expect, it } from 'vitest';
import type { TFunction } from 'i18next';
import type { OAuthProviderDescriptor } from '@/hooks/useOAuthProviders';
import { configuredRoutesFor } from '../ConnectedAccounts';

const t = ((key: string, fallback: string) => {
  const values: Record<string, string> = {
    'setup.regionChina': 'China',
    'setup.regionInternational': 'International',
    'setup.accessTokenPlan': 'Token plan',
    'setup.accessAgentPlan': 'Agent plan',
  };
  return values[key] ?? fallback ?? key;
}) as unknown as TFunction;

const descriptor = {
  id: 'minimax',
  api_key_provider: 'minimax',
  connect_options: [
    { provider: 'minimax', access_type: 'api_key', region: 'cn', plan_kind: null, plan_name: null },
    { provider: 'minimax-intl', access_type: 'api_key', region: 'intl', plan_kind: null, plan_name: null },
    { provider: 'minimax-coding', access_type: 'coding_plan', region: 'cn', plan_kind: 'agent', plan_name: 'M Plan' },
  ],
} as unknown as OAuthProviderDescriptor;

function keys(...providers: string[]) {
  return { providers: providers.map((provider) => ({ provider, has_key: true })) };
}

describe('configuredRoutesFor', () => {
  it('names the exact API region instead of only "configured"', () => {
    expect(configuredRoutesFor(keys('minimax-intl'), descriptor, t)).toEqual(['API International']);
    expect(configuredRoutesFor(keys('minimax'), descriptor, t)).toEqual(['API China']);
  });

  it('names the plan variant for a coding-plan key', () => {
    expect(configuredRoutesFor(keys('minimax-coding'), descriptor, t)).toEqual([
      'Agent plan · M Plan China',
    ]);
  });

  it('lists every saved route and ignores unconfigured providers', () => {
    expect(configuredRoutesFor(keys('minimax', 'minimax-coding'), descriptor, t)).toEqual([
      'API China',
      'Agent plan · M Plan China',
    ]);
    expect(configuredRoutesFor({ providers: [{ provider: 'minimax', has_key: false }] }, descriptor, t)).toEqual([]);
  });
});
