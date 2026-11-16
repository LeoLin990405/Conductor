import { queryOptions, useQuery } from '@tanstack/react-query';

import { getNativeAgents } from '@/api/nativeAgents';
import { queryKeys } from '@/lib/queryKeys';

export const nativeAgentsQuery = queryOptions({
  queryKey: queryKeys.nativeAgents.list(),
  queryFn: getNativeAgents,
  staleTime: 60_000,
  retry: false,
});

/**
 * Host-native CLI inventory. Kept lazy because only a new-thread composer
 * shows the runtime picker; the query is cached for every composer after the
 * first.
 */
export function useNativeAgents(enabled = true) {
  return useQuery({ ...nativeAgentsQuery, enabled });
}
