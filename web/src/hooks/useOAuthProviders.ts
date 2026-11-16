import { useQuery } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import { getOAuthProviders } from '@/lib/dashboardApi/api';
import type { OAuthProviderDescriptor } from '@/lib/dashboardApi/api';

export type { OAuthProviderDescriptor };

/**
 * The connection capability catalog (`GET /api/v1/oauth/providers`) as React
 * Query server state. Every connection surface renders its cards from this, so
 * adding a provider is a backend catalog row, not a new frontend branch.
 *
 * The call is resolved through an arrow on purpose: the surfaces that import
 * this hook also render on deployments that predate the catalog endpoint, and
 * an absent endpoint must read as an empty catalog rather than a module-load
 * failure. `retry: false` keeps that first 404 from being retried.
 */
export function useOAuthProviders(): {
  providers: OAuthProviderDescriptor[];
  isLoading: boolean;
} {
  const { data, isLoading } = useQuery({
    queryKey: queryKeys.oauth.providers(),
    queryFn: () => getOAuthProviders(),
    staleTime: 5 * 60_000,
    retry: false,
  });

  return { providers: data?.providers ?? [], isLoading };
}
