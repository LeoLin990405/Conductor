import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import { useApiKeys } from './useApiKeys';
import {
  getCodexOAuthStatus,
  getClaudeOAuthStatus,
  getGrokOAuthStatus,
  getGeminiOAuthStatus,
  getKimiOAuthStatus,
  getZhipuOAuthStatus,
} from '@/lib/dashboardApi/api';

export interface ConfiguredProvider {
  provider: string;
  display_name: string;
  access_type: 'api_key' | 'oauth' | 'coding_plan' | 'local';
  /** Parent/brand group key — equals ``provider`` when the entry is the brand
   * itself. Carried through so model filters can treat the brand as
   * configured when any of its variants are (e.g. z-ai-coding → z-ai). */
  brand_key?: string;
  /** Connected subscription plan for OAuth providers (e.g. "pro", "team").
   * Consulted by the ``oauth_plans`` per-model gate; null/absent = unknown. */
  plan_type?: string | null;
}

/**
 * Returns a unified list of all configured providers (BYOK keys + OAuth connections).
 * Used by the wizard to show what's already connected and to mark provider cards.
 */
export function useConfiguredProviders() {
  const { apiKeys, isLoading: keysLoading } = useApiKeys();

  const { data: codexStatus, isLoading: codexLoading } = useQuery({
    queryKey: queryKeys.oauth.codex(),
    queryFn: getCodexOAuthStatus,
    staleTime: 60_000,
    retry: false,
  });

  const { data: claudeStatus, isLoading: claudeLoading } = useQuery({
    queryKey: queryKeys.oauth.claude(),
    queryFn: getClaudeOAuthStatus,
    staleTime: 60_000,
    retry: false,
  });

  const { data: grokStatus, isLoading: grokLoading } = useQuery({
    queryKey: queryKeys.oauth.grok(),
    queryFn: getGrokOAuthStatus,
    staleTime: 60_000,
    retry: false,
  });

  const { data: geminiStatus, isLoading: geminiLoading } = useQuery({
    queryKey: queryKeys.oauth.gemini(),
    queryFn: getGeminiOAuthStatus,
    staleTime: 60_000,
    retry: false,
  });

  const { data: kimiStatus, isLoading: kimiLoading } = useQuery({
    queryKey: queryKeys.oauth.kimi(),
    queryFn: getKimiOAuthStatus,
    staleTime: 60_000,
    retry: false,
  });

  const { data: zhipuStatus, isLoading: zhipuLoading } = useQuery({
    queryKey: queryKeys.oauth.zhipu(),
    queryFn: getZhipuOAuthStatus,
    staleTime: 60_000,
    retry: false,
  });

  const providers = useMemo<ConfiguredProvider[]>(() => {
    const result: ConfiguredProvider[] = [];

    // BYOK providers with keys
    if (apiKeys) {
      const keys = apiKeys as Record<string, unknown>;
      if (Array.isArray(keys.providers)) {
        for (const p of keys.providers as Array<{
          provider: string;
          display_name?: string;
          access_type?: string;
          has_key?: boolean;
          brand_key?: string;
        }>) {
          if (p.has_key) {
            result.push({
              provider: p.provider,
              display_name: p.display_name ?? p.provider,
              access_type: (p.access_type as ConfiguredProvider['access_type']) ?? 'api_key',
              brand_key: p.brand_key ?? p.provider,
            });
          }
        }
      }
    }

    // OAuth providers
    if (codexStatus?.connected) {
      result.push({
        provider: 'codex-oauth',
        display_name: 'ChatGPT Codex',
        access_type: 'oauth',
        plan_type: codexStatus.plan_type ?? null,
      });
    }
    if (claudeStatus?.connected) {
      result.push({
        provider: 'claude-oauth',
        display_name: 'Claude (OAuth)',
        access_type: 'oauth',
        plan_type: claudeStatus.plan_type ?? null,
      });
    }
    if (grokStatus?.connected) {
      result.push({
        provider: 'grok-oauth',
        display_name: 'Grok (xAI 登录)',
        access_type: 'oauth',
        plan_type: grokStatus.plan_type ?? null,
      });
    }
    if (geminiStatus?.connected) {
      result.push({
        provider: 'gemini-oauth',
        display_name: 'Gemini (Antigravity 登录)',
        access_type: 'oauth',
        plan_type: geminiStatus.plan_type ?? null,
      });
    }
    if (kimiStatus?.connected) {
      result.push({
        provider: 'kimi-oauth',
        display_name: 'Kimi（订阅登录）',
        access_type: 'oauth',
        plan_type: kimiStatus.plan_type ?? null,
      });
    }
    // One Zhipu connection covers both consoles; the status says which one was
    // used, and that is the provider id the model catalog groups under.
    if (zhipuStatus?.connected) {
      const isZai = zhipuStatus.provider === 'zai';
      result.push({
        provider: isZai ? 'z-ai-oauth' : 'z-ai-cn-oauth',
        display_name: isZai ? 'Z.ai（订阅登录）' : '智谱（订阅登录）',
        access_type: 'oauth',
      });
    }

    return result;
  }, [apiKeys, codexStatus, claudeStatus, grokStatus, geminiStatus, kimiStatus, zhipuStatus]);

  /** Set of configured provider keys for fast lookup */
  const configuredSet = useMemo(
    () => new Set(providers.map((p) => p.provider)),
    [providers],
  );

  return {
    providers,
    configuredSet,
    hasAny: providers.length > 0,
    isLoading: keysLoading || codexLoading || claudeLoading || grokLoading || geminiLoading || kimiLoading || zhipuLoading,
  };
}
