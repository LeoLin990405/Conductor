import { useQuery } from '@tanstack/react-query';
import { queryKeys } from '../lib/queryKeys';
import { useAuth } from '../contexts/AuthContext';
import { getReadiness, type ReadinessResponse } from '../pages/Setup/utils/api';

/**
 * Runtime readiness for the signed-in user.
 *
 * The response is `Cache-Control: private, no-store`, so it is treated as never
 * fresh (`staleTime: 0`) and never persisted. The authenticated user id is part
 * of the query key — that per-user key IS the isolation between accounts, and no
 * extra reset registry is needed: `AuthContext` already calls
 * `queryClient.clear()` on sign-out and on an account switch, which wipes it.
 */
export function useReadiness({ enabled = true }: { enabled?: boolean } = {}) {
  const { userId } = useAuth();
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery<ReadinessResponse>({
    queryKey: queryKeys.system.readiness(userId),
    queryFn: getReadiness,
    staleTime: 0,
    retry: false,
    enabled: enabled && userId != null,
  });

  return {
    readiness: data ?? null,
    isLoading,
    isError,
    error,
    refetch,
    isFetching,
  };
}
