/**
 * The connection capability catalog hook: server state for the descriptor list
 * every connection surface renders from. Pinned here is that it reads the one
 * endpoint, caches for a while, and treats a deployment without the catalog as
 * an empty list rather than an error surface.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { waitFor } from '@testing-library/react';
import { renderHookWithProviders } from '@/test/utils';

const api = vi.hoisted(() => ({ getOAuthProviders: vi.fn() }));

vi.mock('@/lib/dashboardApi/api', () => api);

import { useOAuthProviders, type OAuthProviderDescriptor } from '../useOAuthProviders';

function descriptor(patch: Partial<OAuthProviderDescriptor> = {}): OAuthProviderDescriptor {
  return {
    id: 'kimi-oauth',
    display_name: 'Kimi 订阅登录',
    display_name_en: 'Kimi subscription',
    brand: 'moonshot',
    region: 'cn',
    kind: 'device',
    login_supported: true,
    unavailable_reason: null,
    unavailable_reason_en: null,
    api_key_provider: 'moonshot-coding',
    website_url: null,
    models: [],
    order: 20,
    ...patch,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('useOAuthProviders', () => {
  it('returns the catalog from the providers endpoint', async () => {
    const kimi = descriptor();
    api.getOAuthProviders.mockResolvedValue({ providers: [kimi] });

    const { result } = renderHookWithProviders(() => useOAuthProviders());

    expect(result.current.isLoading).toBe(true);
    expect(result.current.providers).toEqual([]);

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.providers).toEqual([kimi]);
    expect(api.getOAuthProviders).toHaveBeenCalledTimes(1);
  });

  it('treats a missing endpoint as an empty catalog and does not retry it', async () => {
    api.getOAuthProviders.mockRejectedValue(new Error('Request failed with status code 404'));

    const { result } = renderHookWithProviders(() => useOAuthProviders());

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.providers).toEqual([]);
    expect(api.getOAuthProviders).toHaveBeenCalledTimes(1);
  });
});
