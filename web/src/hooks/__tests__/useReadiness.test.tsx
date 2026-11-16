/**
 * Readiness is per-user data served `private, no-store`: its query key carries
 * the authenticated user id, so an account switch can never reuse the other
 * account's answer and a signed-out render asks for nothing.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { act, waitFor } from '@testing-library/react';
import { QueryClient } from '@tanstack/react-query';

import { renderHookWithProviders } from '@/test/utils';
import { queryKeys } from '@/lib/queryKeys';
import type { ReadinessResponse } from '@/pages/Setup/utils/api';

const { authState } = vi.hoisted(() => ({ authState: { userId: 'user-a' as string | null } }));

vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ userId: authState.userId }) }));
vi.mock('@/pages/Setup/utils/api', () => ({ getReadiness: vi.fn() }));

import { getReadiness } from '@/pages/Setup/utils/api';
import { useReadiness } from '../useReadiness';

const mockGetReadiness = getReadiness as Mock;

function payload(summary: string, checkedAt: string): ReadinessResponse {
  return {
    status: 'ready',
    checked_at: checkedAt,
    verification: 'local_checks_only',
    checks: [
      { id: 'database', status: 'ok', required: true, summary, verification: 'observed' },
    ],
  };
}

function testQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

beforeEach(() => {
  vi.clearAllMocks();
  authState.userId = 'user-a';
});

describe('useReadiness', () => {
  it('keys the cache by user: an account switch fetches again and never reuses the previous answer', async () => {
    const client = testQueryClient();
    const forA = payload('answered for A', '2026-10-24T06:21:02Z');
    const forB = payload('answered for B', '2026-10-24T06:22:00Z');
    mockGetReadiness.mockResolvedValueOnce(forA).mockResolvedValueOnce(forB);

    const { result, rerender } = renderHookWithProviders(() => useReadiness(), { queryClient: client });

    await waitFor(() => expect(result.current.readiness?.checks[0].summary).toBe('answered for A'));
    expect(mockGetReadiness).toHaveBeenCalledTimes(1);
    expect(client.getQueryData(queryKeys.system.readiness('user-a'))).toEqual(forA);

    authState.userId = 'user-b';
    rerender();

    await waitFor(() => expect(result.current.readiness?.checks[0].summary).toBe('answered for B'));
    expect(mockGetReadiness).toHaveBeenCalledTimes(2);
    // Two distinct entries: B never adopted A's answer.
    expect(client.getQueryData(queryKeys.system.readiness('user-b'))).toEqual(forB);
    expect(client.getQueryData(queryKeys.system.readiness('user-a'))).toEqual(forA);
  });

  it('issues no fetch while nobody is signed in, then fetches once a user arrives', async () => {
    const client = testQueryClient();
    mockGetReadiness.mockResolvedValue(payload('answered for A', '2026-10-24T06:21:02Z'));
    authState.userId = null;

    const { result, rerender } = renderHookWithProviders(() => useReadiness(), { queryClient: client });

    await act(async () => {});
    expect(mockGetReadiness).not.toHaveBeenCalled();
    expect(result.current.readiness).toBeNull();
    expect(result.current.isLoading).toBe(false);

    authState.userId = 'user-a';
    rerender();

    await waitFor(() => expect(result.current.readiness?.checks[0].summary).toBe('answered for A'));
    expect(mockGetReadiness).toHaveBeenCalledTimes(1);
  });

  it('honours enabled=false even for a signed-in user', async () => {
    const client = testQueryClient();
    renderHookWithProviders(() => useReadiness({ enabled: false }), { queryClient: client });

    await act(async () => {});
    expect(mockGetReadiness).not.toHaveBeenCalled();
  });
});
