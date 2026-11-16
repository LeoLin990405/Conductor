import { useEffect, useId, useRef, useState } from 'react';
import { Link2, Unlink, ExternalLink, Shield, ClipboardCopy } from 'lucide-react';
import { useNavigate } from 'react-router';
import { Input } from '@/components/ui/input';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { initiateCodexDevice, pollCodexDevice, getCodexOAuthStatus, disconnectCodexOAuth, initiateClaudeOAuth, submitClaudeCallback, getClaudeOAuthStatus, disconnectClaudeOAuth, initiateGrokDevice, pollGrokDevice, getGrokOAuthStatus, disconnectGrokOAuth, initiateGeminiOAuth, submitGeminiCallback, getGeminiOAuthStatus, disconnectGeminiOAuth, getKimiOAuthStatus, getZhipuOAuthStatus, getMimoOAuthStatus, getDeepSeekOAuthStatus, getDeepSeekOAuthModels } from '@/lib/dashboardApi/api';
import type { ZhipuProvider } from '@/lib/dashboardApi/api';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { useSubscriptionLogin } from '@/components/model/useSubscriptionLogin';
import { DeviceCodePrompt, RedirectLoginPrompt } from '@/components/model/SubscriptionLoginPanels';
import { useApiKeys } from '@/hooks/useApiKeys';
import { useLocale } from '@/hooks/useLocale';
import { useOAuthProviders } from '@/hooks/useOAuthProviders';
import type { OAuthProviderDescriptor } from '@/hooks/useOAuthProviders';
import '../locales/register';
import './ConnectedAccounts.css';

interface CodexDeviceCode {
  user_code: string;
  verification_url: string;
  interval?: number;
}

interface OAuthStatus {
  connected: boolean;
  account_id?: string | null;
  email?: string | null;
  plan_type?: string | null;
}

// ---------------------------------------------------------------------------
// Unified account connections (descriptor-driven)
// ---------------------------------------------------------------------------

/** The connection status the unified cards read; the per-provider response
 *  shapes differ in extra fields, but every card needs only these. */
interface DomesticProviderStatus {
  connected: boolean;
  account_id?: string | null;
  email?: string | null;
  plan_type?: string | null;
  provider?: ZhipuProvider | null;
}

/**
 * The status endpoint for a descriptor id. Kimi, MiMo and the two Zhipu
 * consoles each keep their own connection record; a descriptor this build has
 * no status endpoint for reads as disconnected (its flow, if any, still runs).
 */
function statusQueryFor(providerId: string): {
  queryKey: readonly unknown[];
  queryFn: () => Promise<DomesticProviderStatus>;
} | null {
  switch (providerId) {
    case 'kimi-oauth':
      return { queryKey: queryKeys.oauth.kimi(), queryFn: getKimiOAuthStatus };
    case 'z-ai-cn-oauth':
    case 'z-ai-oauth':
      return { queryKey: queryKeys.oauth.zhipu(), queryFn: getZhipuOAuthStatus };
    case 'mimo-oauth':
      return { queryKey: queryKeys.oauth.mimo(), queryFn: getMimoOAuthStatus };
    case 'deepseek-account':
      return {
        queryKey: queryKeys.oauth.deepseek(),
        // The DeepSeek status endpoint only reports the connection bit.
        queryFn: async () => ({ ...(await getDeepSeekOAuthStatus()) }) as DomesticProviderStatus,
      };
    default:
      return null;
  }
}

/**
 * The brands whose subscription accounts this app's users hold. Region is
 * endpoint geography, not who the account belongs to: Z.ai signs in through its
 * international console but serves the same 智谱 family, so it sits beside
 * BigModel here instead of vanishing when the four international cards keep
 * their own list.
 */
const DOMESTIC_BRANDS = new Set(['moonshot', 'z-ai', 'z-ai-cn', 'mimo', 'deepseek']);

function descriptorName(descriptor: OAuthProviderDescriptor, locale: string): string {
  const chinese = locale.toLowerCase().startsWith('zh');
  const preferred = chinese ? descriptor.display_name : descriptor.display_name_en;
  return preferred || descriptor.display_name_en || descriptor.display_name || descriptor.id;
}

/** The "no account login" explanation in the reader's language, if any. */
/**
 * The connect-step state for a descriptor's API-key fallback. Settings otherwise
 * only knows the BYOK provider id, which left the API-key editor with an empty
 * Base URL and a generic "API" format; the catalog now supplies the manifest
 * sdk/base URL/region defaults.
 */
function apiKeyConnectState(descriptor: OAuthProviderDescriptor, name: string) {
  const defaults = descriptor.connect_defaults ?? null;
  return {
    method: 'api_key' as const,
    provider: descriptor.api_key_provider ?? '',
    displayName: name,
    brandKey: descriptor.brand,
    sdk: defaults?.sdk ?? null,
    defaultBaseUrl: defaults?.base_url ?? null,
    useResponseApi: defaults?.use_response_api ?? false,
    regionVariants: defaults?.region_variants ?? null,
    defaultRegion: defaults?.region ?? null,
    dynamicModels: defaults?.dynamic_models ?? false,
  };
}

type ConnectOption = NonNullable<OAuthProviderDescriptor['connect_options']>[number];

function connectOptionState(descriptor: OAuthProviderDescriptor, name: string, option: ConnectOption) {
  return {
    method: option.access_type === 'coding_plan' ? 'coding_plan' as const : 'api_key' as const,
    provider: option.provider,
    displayName: name,
    brandKey: descriptor.brand,
    sdk: option.sdk ?? null,
    defaultBaseUrl: option.base_url ?? null,
    useResponseApi: option.use_response_api ?? false,
    defaultRegion: option.region ?? null,
    planKind: option.plan_kind ?? null,
    planName: option.plan_name ?? null,
    accessType: option.access_type,
    dynamicModels: option.dynamic_models ?? false,
  };
}

function connectOptionLabel(option: ConnectOption, t: TFunction): string {
  const region = option.region === 'cn'
    ? t('setup.regionChina', 'China')
    : t('setup.regionInternational', 'International');
  if (option.access_type === 'api_key') return `API ${region}`;
  const kind = option.plan_kind === 'token'
    ? t('setup.accessTokenPlan', 'Token plan')
    : t('setup.accessAgentPlan', 'Agent plan');
  return `${kind}${option.plan_name ? ` · ${option.plan_name}` : ''} ${region}`;
}

function descriptorUnavailableReason(
  descriptor: OAuthProviderDescriptor,
  locale: string,
): string | null {
  if (locale.toLowerCase().startsWith('zh')) return descriptor.unavailable_reason;
  return descriptor.unavailable_reason_en ?? descriptor.unavailable_reason;
}

/** The BYOK providers with a key, as the API-keys response reports them. */
function configuredApiProviders(apiKeys: Record<string, unknown> | null): string[] {
  if (!apiKeys || !Array.isArray(apiKeys.providers)) return [];
  return (apiKeys.providers as Array<{ provider?: unknown; has_key?: unknown }>)
    .filter((row) => row && typeof row === 'object' && row.has_key === true)
    .map((row) => String(row.provider ?? ''))
    .filter(Boolean);
}

/**
 * The user-facing route labels for a provider's configured credentials. A
 * provider id is not a connection: `minimax-coding` is the M Plan, while
 * `minimax` and `minimax-intl` are two different API regions. Settings must
 * say which one saved, not just "configured".
 */
export function configuredRoutesFor(
  apiKeys: Record<string, unknown> | null,
  descriptor: OAuthProviderDescriptor,
  t: TFunction,
): string[] {
  const configured = configuredApiProviders(apiKeys);
  const options = descriptor.connect_options ?? [];
  return configured
    .map((provider) => {
      const option = options.find((candidate) => candidate.provider === provider);
      if (option) return connectOptionLabel(option, t);
      if (provider === descriptor.api_key_provider) return t('settings.apiKeyConfigured', 'API key set');
      return null;
    })
    .filter((label): label is string => Boolean(label));
}

/**
 * The account connections this app offers beyond the four international cards:
 * one card per domestic login descriptor (Kimi, 智谱 BigModel, Z.ai, MiMo) and a
 * row per API-key-only provider. Everything is read from the backend capability
 * catalog, so a new provider is a catalog row, not a new card here.
 */
function DomesticConnections() {
  const { t } = useTranslation();
  const locale = useLocale();
  const headingId = useId();
  const { providers, isLoading } = useOAuthProviders();
  const { apiKeys } = useApiKeys();

  const loginDescriptors = providers
    .filter(
      (descriptor) =>
        descriptor.login_supported &&
        descriptor.kind !== 'api_key_only' &&
        DOMESTIC_BRANDS.has(descriptor.brand),
    )
    .slice()
    .sort((a, b) => a.order - b.order);
  const apiKeyDescriptors = providers
    .filter((descriptor) => descriptor.kind === 'api_key_only')
    .slice()
    .sort((a, b) => a.order - b.order);

  // A deployment without the catalog endpoint (or before its first response)
  // keeps the pre-existing cards only; the section appears with the catalog.
  if (isLoading || (loginDescriptors.length === 0 && apiKeyDescriptors.length === 0)) {
    return null;
  }

  const configuredSummary = apiKeyDescriptors.flatMap((descriptor) =>
    configuredRoutesFor(apiKeys, descriptor, t).map((route) => ({
      key: `${descriptor.id}:${route}`,
      name: descriptorName(descriptor, locale),
      route,
    })),
  );

  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-2">
      <div>
        <h3
          id={headingId}
          className="text-sm font-medium"
          style={{ color: 'var(--color-text-primary)' }}
        >
          {t('settings.accountConnections')}
        </h3>
        <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('settings.accountConnectionsDesc')}
        </p>
      </div>
      {configuredSummary.length > 0 && (
        <div className="configured-routes" data-testid="configured-routes">
          <span className="configured-routes-label">
            {t('settings.configuredSummary', '已配置连接')}
          </span>
          <div className="configured-routes-list">
            {configuredSummary.map((item) => (
              <span key={item.key} className="configured-route-chip">
                {item.name} <span>·</span> {item.route}
              </span>
            ))}
          </div>
        </div>
      )}
      {loginDescriptors.map((descriptor) => (
        <DomesticLoginCard key={descriptor.id} descriptor={descriptor} />
      ))}
      {apiKeyDescriptors.map((descriptor) => (
        <DomesticApiKeyRow
          key={descriptor.id}
          descriptor={descriptor}
          configuredRoutes={configuredRoutesFor(apiKeys, descriptor, t)}
        />
      ))}
    </section>
  );
}

/**
 * One login descriptor as a card: title, connection status, the shared sign-in
 * flow, and the API-key alternative for the same quota. The waiting language
 * and the flow mechanics come from the shared hook/panels so Settings and Setup
 * cannot drift.
 */
function DomesticLoginCard({ descriptor }: { descriptor: OAuthProviderDescriptor }) {
  const { t } = useTranslation();
  const locale = useLocale();
  const navigate = useNavigate();
  const pastePromptId = useId();
  const name = descriptorName(descriptor, locale);
  const [pasteInput, setPasteInput] = useState('');
  const statusQuery = statusQueryFor(descriptor.id);
  const { data: status } = useQuery({
    queryKey: statusQuery?.queryKey ?? [...queryKeys.oauth.all, 'status', descriptor.id],
    queryFn: statusQuery?.queryFn ?? (async () => null),
    enabled: statusQuery !== null,
    staleTime: 60_000,
    retry: false,
  });
  const usageHint =
    (locale.toLowerCase().startsWith('zh') ? descriptor.usage_hint : descriptor.usage_hint_en) ??
    descriptor.usage_hint ??
    null;
  const login = useSubscriptionLogin(descriptor.id);
  // One Zhipu connection serves either console, and the status reports which
  // one signed in: the other console's card must not claim that connection.
  const otherConsoleConnected =
    (descriptor.id === 'z-ai-oauth' && status?.provider === 'bigmodel') ||
    (descriptor.id === 'z-ai-cn-oauth' && status?.provider === 'zai');
  const connected = status?.connected === true && !otherConsoleConnected;
  const accountLine = connected
    ? [
        status?.provider === 'zai'
          ? t('settings.zhipuProviderZai')
          : status?.provider === 'bigmodel'
            ? t('settings.zhipuProviderBigmodel')
            : null,
        status?.email || status?.account_id || null,
      ]
        .filter((part): part is string => Boolean(part))
        .join(' · ') || t('settings.connected')
    : null;

  // DeepSeek's account exposes its own Platform catalog; show what that account
  // can actually run instead of only the built-in fallback rows.
  const deepseekModelsQuery = useQuery({
    queryKey: queryKeys.oauth.deepseekModels(),
    queryFn: getDeepSeekOAuthModels,
    enabled: descriptor.id === 'deepseek-account' && connected,
    staleTime: 5 * 60_000,
    retry: false,
  });
  const deepseekModelNames =
    descriptor.id === 'deepseek-account' && connected
      ? (deepseekModelsQuery.data?.models ?? [])
          .map((row) => row.display_name || row.name || row.id)
          .filter((name): name is string => typeof name === 'string' && name.length > 0)
      : [];

  const handlePasteSubmit = async () => {
    const completed = await login.submit(pasteInput);
    if (completed) setPasteInput('');
  };

  const connectButtonClass =
    'flex items-center gap-1.5 px-3 py-1.5 rounded-(--radius-control) text-xs font-medium transition-opacity hover:opacity-90';
  const connectButtonStyle = {
    backgroundColor: login.starting ? 'var(--color-bg-elevated)' : 'var(--color-btn-primary-bg)',
    color: login.starting ? 'var(--color-text-tertiary)' : 'var(--color-btn-primary-text)',
  } as const;

  return (
    <div
      className="tn-settings-card rounded-(--radius-card) px-4 py-3"
      style={{
        backgroundColor: 'var(--color-bg-card)',
        border: `1px solid ${connected ? 'var(--color-success-soft)' : 'var(--color-border-muted)'}`,
      }}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3 min-w-0">
          <div
            className="h-8 w-8 rounded-(--radius-control) flex items-center justify-center shrink-0"
            style={{
              backgroundColor: connected ? 'var(--color-success-soft)' : 'var(--color-accent-soft)',
            }}
          >
            <Link2
              className="h-4 w-4"
              style={{ color: connected ? 'var(--color-success)' : 'var(--color-accent-primary)' }}
            />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
                {name}
              </span>
              {connected && status?.plan_type && (
                <span
                  className="inline-flex items-center px-1.5 py-0.5 rounded-(--radius-control) text-(length:--size-chrome) font-medium"
                  style={{
                    backgroundColor: 'var(--color-success-soft)',
                    color: 'var(--color-success)',
                  }}
                >
                  {status.plan_type}
                </span>
              )}
            </div>
            <p className="text-xs mt-0.5 break-all" style={{ color: 'var(--color-text-tertiary)' }}>
              {connected
                ? accountLine
                : t('settings.subscriptionLoginDesc', { provider: name })}
            </p>
          </div>
        </div>
        <div className="shrink-0">
          {connected ? (
            <button
              type="button"
              onClick={() => { void login.disconnect(); }}
              disabled={login.disconnecting}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-(--radius-control) text-xs font-medium transition-colors"
              style={{
                color: 'var(--color-loss)',
                backgroundColor: 'transparent',
                border: '1px solid var(--color-loss)',
              }}
            >
              <Unlink className="h-3 w-3" />
              {login.disconnecting
                ? t('common.loading')
                : t('settings.disconnect')}
            </button>
          ) : !login.awaiting ? (
            <button
              type="button"
              onClick={() => { void login.start(); }}
              disabled={login.starting}
              className={connectButtonClass}
              style={connectButtonStyle}
            >
              <Link2 className="h-3 w-3" />
              {login.starting ? t('common.loading') : t('settings.connect')}
            </button>
          ) : null}
        </div>
      </div>

      {!connected && login.awaiting && login.kind === 'device' && login.deviceCode && (
        <div className="mt-3 pt-3" style={{ borderTop: '1px solid var(--color-border-muted)' }}>
          <DeviceCodePrompt
            deviceCode={login.deviceCode}
            polling={login.polling}
            error={login.error}
            onCancel={login.cancel}
          />
        </div>
      )}

      {!connected && usageHint && (
        <p
          className="text-xs mt-2 leading-relaxed"
          style={{ color: 'var(--color-warning)' }}
        >
          {usageHint}
          {descriptor.usage_policy_url && (
            <>
              {' '}
              <a
                href={descriptor.usage_policy_url}
                target="_blank"
                rel="noopener noreferrer"
                className="underline underline-offset-2 hover:opacity-80"
              >
                {locale.toLowerCase().startsWith('zh') ? '使用条款' : 'Policy'}
              </a>
            </>
          )}
        </p>
      )}

      {connected && deepseekModelNames.length > 0 && (
        <p
          className="text-xs mt-1.5 break-words"
          style={{ color: 'var(--color-text-tertiary)' }}
        >
          {t('settings.deepseekAccountModels', {
            models: deepseekModelNames.join(' · '),
          })}
        </p>
      )}

      {!connected && login.awaiting && (login.kind === 'redirect' || login.kind === 'deepseek') && (
        <div className="mt-3 pt-3" style={{ borderTop: '1px solid var(--color-border-muted)' }}>
          <RedirectLoginPrompt
            authorizeUrl={login.authorizeUrl}
            polling={login.polling}
            error={login.error}
            onCancel={login.cancel}
          />
        </div>
      )}

      {!connected && login.awaiting && login.kind === 'paste' && login.authorizeUrl && (
        <div
          className="mt-3 pt-3 flex flex-col gap-2"
          style={{ borderTop: '1px solid var(--color-border-muted)' }}
        >
          <p id={pastePromptId} className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
            {t('settings.pasteCodePrompt')}
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <Input
              aria-labelledby={pastePromptId}
              aria-invalid={login.error !== null}
              value={pasteInput}
              onChange={(event) => setPasteInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') void handlePasteSubmit();
              }}
              placeholder={t('settings.pasteCodePlaceholder')}
              className="flex-1 min-w-0 font-mono text-xs"
              autoComplete="off"
              spellCheck={false}
            />
            <button
              type="button"
              onClick={() => { void handlePasteSubmit(); }}
              disabled={login.submitting || !pasteInput.trim()}
              className={connectButtonClass}
              style={{
                backgroundColor: login.submitting
                  ? 'var(--color-bg-elevated)'
                  : 'var(--color-btn-primary-bg)',
                color: login.submitting
                  ? 'var(--color-text-tertiary)'
                  : 'var(--color-btn-primary-text)',
              }}
            >
              {login.submitting ? t('common.loading') : t('common.submit')}
            </button>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <a
              href={login.authorizeUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs underline underline-offset-2 break-all hover:opacity-80"
              style={{ color: 'var(--color-accent-primary)' }}
            >
              {t('subscription.openLoginAgain')}
            </a>
            <button
              type="button"
              onClick={login.cancel}
              className="px-2 py-1 rounded-(--radius-control) text-xs font-medium"
              style={{ color: 'var(--color-text-tertiary)', backgroundColor: 'transparent' }}
            >
              {t('common.cancel')}
            </button>
          </div>
          {login.error && (
            <p role="alert" className="text-xs" style={{ color: 'var(--color-loss)' }}>
              {login.error}
            </p>
          )}
        </div>
      )}

      {!connected && !login.awaiting && login.error && (
        <p role="alert" className="text-xs mt-1.5" style={{ color: 'var(--color-loss)' }}>
          {login.error}
        </p>
      )}

      {!connected && !login.awaiting && descriptor.api_key_provider && (
        <button
          type="button"
          onClick={() =>
            navigate('/setup/connect', {
              state: apiKeyConnectState(descriptor, name),
            })
          }
          className="mt-2 text-xs underline underline-offset-2 hover:opacity-80"
          style={{ color: 'var(--color-text-tertiary)' }}
        >
          {t('settings.orEnterApiKey')}
        </button>
      )}
    </div>
  );
}

/**
 * A provider with no account login: its connection state is the presence of a
 * BYOK key, the primary action opens the API-key step, the secondary link goes
 * to the provider's own key page, and the muted line explains why a login is
 * not offered.
 */
function DomesticApiKeyRow({
  descriptor,
  configuredRoutes,
}: {
  descriptor: OAuthProviderDescriptor;
  configuredRoutes: string[];
}) {
  const { t } = useTranslation();
  const locale = useLocale();
  const navigate = useNavigate();
  const name = descriptorName(descriptor, locale);
  const reason = descriptorUnavailableReason(descriptor, locale);

  return (
    <div
      className="tn-settings-card rounded-(--radius-card) px-4 py-3 flex flex-wrap items-center justify-between gap-2"
      style={{
        backgroundColor: 'var(--color-bg-card)',
        border: '1px solid var(--color-border-muted)',
      }}
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {name}
          </span>
          {configuredRoutes.length > 0 ? (
            configuredRoutes.map((route) => (
              <span
                key={route}
                className="inline-flex items-center px-1.5 py-0.5 rounded-(--radius-control) text-(length:--size-chrome) font-medium"
                style={{
                  backgroundColor: 'var(--color-success-soft)',
                  color: 'var(--color-success)',
                }}
              >
                {route === t('settings.apiKeyConfigured', 'API key set')
                  ? route
                  : t('settings.apiKeyConfiguredAs', '已配置 · {{route}}', { route })}
              </span>
            ))
          ) : (
            <span
              className="inline-flex items-center px-1.5 py-0.5 rounded-(--radius-control) text-(length:--size-chrome) font-medium"
              style={{ backgroundColor: 'var(--color-bg-tag)', color: 'var(--color-text-tertiary)' }}
            >
              {t('settings.apiKeyMissing')}
            </span>
          )}
        </div>
        {reason && (
          <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
            {reason}
          </p>
        )}
        {configuredRoutes.length > 0 && (
          <p className="configured-current-route">
            {t('settings.apiKeyConfiguredRoute', '当前连接：{{route}}', {
              route: configuredRoutes.join(' · '),
            })}
          </p>
        )}
      </div>
      <div className="flex flex-wrap items-center justify-end gap-2">
        {descriptor.api_key_provider && (
          (descriptor.connect_options?.length ?? 0) > 1 ? (
            <div className="flex flex-col items-end gap-1">
              <span className="text-(length:--size-chrome)" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('settings.chooseConnection', 'Choose a route')}
              </span>
              <div className="flex flex-wrap justify-end gap-1.5">
                {descriptor.connect_options!.map((option) => (
                  <button
                    key={option.provider}
                    type="button"
                    onClick={() =>
                      navigate('/setup/connect', {
                        state: connectOptionState(descriptor, name, option),
                      })
                    }
                    className="rounded-(--radius-control) px-2 py-1 text-(length:--size-chrome) font-medium transition-opacity hover:opacity-85"
                    style={{
                      backgroundColor: option.access_type === 'api_key'
                        ? 'var(--color-bg-tag)'
                        : 'var(--color-accent-soft)',
                      color: option.access_type === 'api_key'
                        ? 'var(--color-text-secondary)'
                        : 'var(--color-accent-primary)',
                      border: `1px solid ${option.access_type === 'api_key' ? 'var(--color-border-default)' : 'var(--color-accent-overlay)'}`,
                    }}
                  >
                    {connectOptionLabel(option, t)}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <button
              type="button"
              onClick={() =>
                navigate('/setup/connect', {
                  state: apiKeyConnectState(descriptor, name),
                })
              }
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-(--radius-control) text-xs font-medium transition-opacity hover:opacity-90"
              style={{
                backgroundColor: 'var(--color-btn-primary-bg)',
                color: 'var(--color-btn-primary-text)',
              }}
            >
              {t('settings.enterApiKey')}
            </button>
          )
        )}
        {descriptor.website_url && (
          <a
            href={descriptor.website_url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-xs underline underline-offset-2 hover:opacity-80"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {t('settings.getKeyFromWebsite')}
            <ExternalLink className="h-3 w-3" aria-hidden="true" />
          </a>
        )}
      </div>
    </div>
  );
}

/** Connected Accounts section of the model tab: the Codex device-code flow and
 * the Claude PKCE paste-back flow, each with its disclaimer dialog. Owns its
 * OAuth state end-to-end — fetches connection status when the section mounts
 * (i.e. when the model tab is first shown). */
export function ConnectedAccounts() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  // Connected Accounts (Codex OAuth — Device Code Flow)
  const [codexOAuthStatus, setCodexOAuthStatus] = useState<OAuthStatus>({ connected: false });
  const [showCodexDisclaimer, setShowCodexDisclaimer] = useState(false);
  const [isConnectingCodex, setIsConnectingCodex] = useState(false);
  const [isDisconnectingCodex, setIsDisconnectingCodex] = useState(false);
  const [codexDeviceCode, setCodexDeviceCode] = useState<CodexDeviceCode | null>(null);
  const [codexDeviceError, setCodexDeviceError] = useState<string | null>(null);
  const [isPollingCodex, setIsPollingCodex] = useState(false);
  const codexPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Connected Accounts (Claude OAuth — PKCE Authorization Code Flow)
  const [claudeOAuthStatus, setClaudeOAuthStatus] = useState<OAuthStatus>({ connected: false });
  const [showClaudeDisclaimer, setShowClaudeDisclaimer] = useState(false);
  const [isConnectingClaude, setIsConnectingClaude] = useState(false);
  const [isDisconnectingClaude, setIsDisconnectingClaude] = useState(false);
  const [claudeAuthorizeUrl, setClaudeAuthorizeUrl] = useState<string | null>(null);
  const [claudeCallbackInput, setClaudeCallbackInput] = useState('');
  const [claudeError, setClaudeError] = useState<string | null>(null);
  const [isSubmittingClaudeCallback, setIsSubmittingClaudeCallback] = useState(false);

  const [accountsError, setAccountsError] = useState<string | null>(null);

  // Grok OAuth (xAI device code)
  const [grokOAuthStatus, setGrokOAuthStatus] = useState<OAuthStatus>({ connected: false });
  const [isConnectingGrok, setIsConnectingGrok] = useState(false);
  const [isDisconnectingGrok, setIsDisconnectingGrok] = useState(false);
  const [grokDeviceCode, setGrokDeviceCode] = useState<CodexDeviceCode | null>(null);
  const [grokDeviceError, setGrokDeviceError] = useState<string | null>(null);
  const [isPollingGrok, setIsPollingGrok] = useState(false);
  const grokPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Gemini OAuth (Google account, Gemini Code Assist)
  const [geminiOAuthStatus, setGeminiOAuthStatus] = useState<OAuthStatus>({ connected: false });
  const [isConnectingGemini, setIsConnectingGemini] = useState(false);
  const [isDisconnectingGemini, setIsDisconnectingGemini] = useState(false);
  const [geminiAuthorizeUrl, setGeminiAuthorizeUrl] = useState<string | null>(null);
  const [geminiCallbackInput, setGeminiCallbackInput] = useState('');
  const [geminiError, setGeminiError] = useState<string | null>(null);
  const [isSubmittingGemini, setIsSubmittingGemini] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [codexStatus, claudeStatus, grokStatus, geminiStatus] = await Promise.all([
          getCodexOAuthStatus(),
          getClaudeOAuthStatus(),
          getGrokOAuthStatus(),
          getGeminiOAuthStatus(),
        ]) as [OAuthStatus, OAuthStatus, OAuthStatus, OAuthStatus];
        if (cancelled) return;
        setCodexOAuthStatus(codexStatus || { connected: false });
        setClaudeOAuthStatus(claudeStatus || { connected: false });
        setGrokOAuthStatus(grokStatus || { connected: false });
        setGeminiOAuthStatus(geminiStatus || { connected: false });
      } catch {
        // Status stays disconnected; the connect flows still work.
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Cleanup device code polling on unmount
  useEffect(() => {
    return () => {
      if (codexPollRef.current) {
        clearInterval(codexPollRef.current);
        codexPollRef.current = null;
      }
      if (grokPollRef.current) {
        clearInterval(grokPollRef.current);
        grokPollRef.current = null;
      }
    };
  }, []);

  const handleCodexConnectClick = () => {
    setShowCodexDisclaimer(true);
  };

  // Declared before handleCodexConnect: when a handler refers to a function
  // declared after it, react-hooks/purity reads the handler's Date.now() as a
  // render-time call.
  const handleCodexDeviceCancel = () => {
    if (codexPollRef.current) {
      clearInterval(codexPollRef.current);
      codexPollRef.current = null;
    }
    setIsPollingCodex(false);
    setCodexDeviceCode(null);
    setCodexDeviceError(null);
  };

  const handleCodexConnect = async () => {
    setShowCodexDisclaimer(false);
    setIsConnectingCodex(true);
    setAccountsError(null);
    setCodexDeviceError(null);
    try {
      const device = await initiateCodexDevice() as unknown as CodexDeviceCode;
      setCodexDeviceCode(device);
      // Open verification URL in new tab
      window.open(device.verification_url, '_blank', 'noopener');
      // Start polling
      setIsPollingCodex(true);
      const interval = (device.interval || 5) * 1000;
      const startTime = Date.now();
      const maxDuration = 15 * 60 * 1000; // 15 minutes
      codexPollRef.current = setInterval(async () => {
        if (Date.now() - startTime > maxDuration) {
          handleCodexDeviceCancel();
          setCodexDeviceError(t('settings.codexTimeout'));
          return;
        }
        try {
          const result = await pollCodexDevice() as Record<string, unknown>;
          if (result.success) {
            handleCodexDeviceCancel(); // stop polling
            setCodexOAuthStatus({
              connected: true,
              account_id: result.account_id as string,
              email: result.email as string,
              plan_type: result.plan_type as string,
            });
            queryClient.invalidateQueries({ queryKey: queryKeys.oauth.codex() });
            queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
          }
          // result.pending → keep polling
        } catch {
          handleCodexDeviceCancel();
          setCodexDeviceError(t('settings.codexPollFailed'));
        }
      }, interval);
    } catch {
      setAccountsError(t('settings.codexFlowFailed'));
    } finally {
      setIsConnectingCodex(false);
    }
  };

  const handleCodexDisconnect = async () => {
    setIsDisconnectingCodex(true);
    setAccountsError(null);
    try {
      await disconnectCodexOAuth();
      setCodexOAuthStatus({ connected: false, account_id: null, email: null, plan_type: null });
      queryClient.invalidateQueries({ queryKey: queryKeys.oauth.codex() });
      queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
    } catch {
      setAccountsError('Failed to disconnect Codex');
    } finally {
      setIsDisconnectingCodex(false);
    }
  };

  // --- Claude OAuth handlers ---

  const handleClaudeConnectClick = () => {
    setShowClaudeDisclaimer(true);
  };

  const handleClaudeConnect = async () => {
    setShowClaudeDisclaimer(false);
    setIsConnectingClaude(true);
    setAccountsError(null);
    setClaudeError(null);
    try {
      const result = await initiateClaudeOAuth() as Record<string, unknown>;
      setClaudeAuthorizeUrl(result.authorize_url as string);
      // Open authorization page in new tab
      window.open(result.authorize_url as string, '_blank', 'noopener');
    } catch {
      setAccountsError(t('settings.claudeConnectFailed', 'Failed to initiate Claude OAuth'));
    } finally {
      setIsConnectingClaude(false);
    }
  };

  const handleClaudeCallbackSubmit = async () => {
    if (!claudeCallbackInput.trim()) return;
    setIsSubmittingClaudeCallback(true);
    setClaudeError(null);
    try {
      const result = await submitClaudeCallback(claudeCallbackInput.trim()) as Record<string, unknown>;
      if (result.success) {
        setClaudeAuthorizeUrl(null);
        setClaudeCallbackInput('');
        setClaudeOAuthStatus({
          connected: true,
          account_id: (result.account_id as string) || '',
          email: (result.email as string) || null,
          plan_type: (result.plan_type as string) || null,
        });
        queryClient.invalidateQueries({ queryKey: queryKeys.oauth.claude() });
        queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
      }
    } catch (e: unknown) {
      const apiError = e as { response?: { data?: { detail?: string } } };
      setClaudeError(apiError.response?.data?.detail || t('settings.claudePasteError', 'Failed to exchange code. Please try again.'));
    } finally {
      setIsSubmittingClaudeCallback(false);
    }
  };

  const handleClaudeCancel = () => {
    setClaudeAuthorizeUrl(null);
    setClaudeCallbackInput('');
    setClaudeError(null);
  };

  const handleClaudeDisconnect = async () => {
    setIsDisconnectingClaude(true);
    setAccountsError(null);
    try {
      await disconnectClaudeOAuth();
      setClaudeOAuthStatus({ connected: false, account_id: null, email: null, plan_type: null });
      queryClient.invalidateQueries({ queryKey: queryKeys.oauth.claude() });
      queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
    } catch {
      setAccountsError('Failed to disconnect Claude');
    } finally {
      setIsDisconnectingClaude(false);
    }
  };

  // --- Grok OAuth handlers ---

  const handleGrokDeviceCancel = () => {
    if (grokPollRef.current) {
      clearInterval(grokPollRef.current);
      grokPollRef.current = null;
    }
    setIsPollingGrok(false);
    setGrokDeviceCode(null);
    setGrokDeviceError(null);
  };

  const handleGrokConnect = async () => {
    setIsConnectingGrok(true);
    setAccountsError(null);
    setGrokDeviceError(null);
    try {
      const device = await initiateGrokDevice() as unknown as CodexDeviceCode;
      setGrokDeviceCode(device);
      window.open(device.verification_url, '_blank', 'noopener');
      setIsPollingGrok(true);
      const interval = (device.interval || 5) * 1000;
      const startTime = Date.now();
      grokPollRef.current = setInterval(async () => {
        if (Date.now() - startTime > 15 * 60 * 1000) {
          handleGrokDeviceCancel();
          setGrokDeviceError(t('settings.grokTimeout', 'Authorization timed out. Please retry.'));
          return;
        }
        try {
          const result = await pollGrokDevice();
          if (result.success) {
            handleGrokDeviceCancel();
            setGrokOAuthStatus({
              connected: true,
              account_id: (result.account_id as string) || '',
              email: (result.email as string) || null,
              plan_type: null,
            });
            queryClient.invalidateQueries({ queryKey: queryKeys.oauth.grok() });
            queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
          }
        } catch {
          handleGrokDeviceCancel();
          setGrokDeviceError(t('settings.grokPollFailed', 'Sign-in failed. Please retry.'));
        }
      }, interval);
    } catch {
      setAccountsError(t('settings.grokConnectFailed', 'Failed to start xAI sign-in'));
    } finally {
      setIsConnectingGrok(false);
    }
  };

  const handleGrokDisconnect = async () => {
    setIsDisconnectingGrok(true);
    setAccountsError(null);
    try {
      await disconnectGrokOAuth();
      setGrokOAuthStatus({ connected: false, account_id: null, email: null, plan_type: null });
      queryClient.invalidateQueries({ queryKey: queryKeys.oauth.grok() });
      queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
    } catch {
      setAccountsError('Failed to disconnect Grok');
    } finally {
      setIsDisconnectingGrok(false);
    }
  };

  // --- Gemini OAuth handlers ---

  const handleGeminiConnect = async () => {
    setIsConnectingGemini(true);
    setAccountsError(null);
    setGeminiError(null);
    try {
      const result = await initiateGeminiOAuth() as Record<string, unknown>;
      setGeminiAuthorizeUrl(result.authorize_url as string);
      window.open(result.authorize_url as string, '_blank', 'noopener');
    } catch {
      setAccountsError(t('settings.geminiConnectFailed', 'Failed to start Google sign-in'));
    } finally {
      setIsConnectingGemini(false);
    }
  };

  const handleGeminiSubmit = async () => {
    if (!geminiCallbackInput.trim()) return;
    setIsSubmittingGemini(true);
    setGeminiError(null);
    try {
      const result = await submitGeminiCallback(geminiCallbackInput.trim()) as Record<string, unknown>;
      if (result.success) {
        setGeminiAuthorizeUrl(null);
        setGeminiCallbackInput('');
        setGeminiOAuthStatus({
          connected: true,
          account_id: (result.account_id as string) || '',
          email: (result.email as string) || null,
          plan_type: (result.plan_type as string) || null,
        });
        queryClient.invalidateQueries({ queryKey: queryKeys.oauth.gemini() });
        queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
      }
    } catch (e: unknown) {
      const apiError = e as {
        response?: {
          data?: {
            detail?:
              | string
              | { code?: string; message?: string; docs_url?: string | null };
          };
        };
      };
      const detail = apiError.response?.data?.detail;
      if (typeof detail === 'string') {
        setGeminiError(detail);
      } else if (
        detail?.code === 'gemini_consumer_oauth_retired' ||
        detail?.code === 'gemini_ineligible'
      ) {
        setGeminiError(
          t(
            'settings.geminiConsumerRetiredError',
            'Google retired Gemini Code Assist sign-in for consumer accounts. OAuth cannot connect this account; use a Gemini API key instead.',
          ),
        );
      } else if (detail?.code === 'gemini_project_required') {
        setGeminiError(
          t(
            'settings.geminiProjectRequired',
            'This account needs a Google Cloud project. Set GOOGLE_CLOUD_PROJECT and sign in again.',
          ),
        );
      } else {
        setGeminiError(
          detail?.message ||
            t('settings.geminiPasteError', 'Failed to exchange the code. Please try again.'),
        );
      }
    } finally {
      setIsSubmittingGemini(false);
    }
  };

  const handleGeminiDisconnect = async () => {
    setIsDisconnectingGemini(true);
    setAccountsError(null);
    try {
      await disconnectGeminiOAuth();
      setGeminiOAuthStatus({ connected: false, account_id: null, email: null, plan_type: null });
      queryClient.invalidateQueries({ queryKey: queryKeys.oauth.gemini() });
      queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
    } catch {
      setAccountsError('Failed to disconnect Gemini');
    } finally {
      setIsDisconnectingGemini(false);
    }
  };

  return (
    <>
      {/* Account connections, rendered from the backend capability catalog. */}
      <DomesticConnections />

      {/* Section 2: Connected Accounts */}
      <div style={{ borderTop: '1px solid var(--color-border-muted)', paddingTop: '16px' }}>
        <label className="block text-sm font-medium mb-1" style={{ color: 'var(--color-text-primary)' }}>
          {t('settings.connectedAccounts', 'Connected Accounts')}
        </label>
        <p className="text-xs mb-3" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('settings.connectedAccountsDesc', 'Connect external accounts to use models through your existing subscriptions.')}
        </p>

        {/* ChatGPT Codex card */}
        <div
          className="tn-settings-card rounded-(--radius-card) px-4 py-3"
          style={{
            backgroundColor: 'var(--color-bg-card)',
            border: `1px solid ${codexOAuthStatus.connected ? 'var(--color-success-soft)' : 'var(--color-border-muted)'}`,
          }}
        >
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div
                className="h-8 w-8 rounded-(--radius-control) flex items-center justify-center"
                style={{ backgroundColor: codexOAuthStatus.connected ? 'var(--color-success-soft)' : 'var(--color-accent-soft)' }}
              >
                <Link2 className="h-4 w-4" style={{ color: codexOAuthStatus.connected ? 'var(--color-success)' : 'var(--color-accent-primary)' }} />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>ChatGPT Codex</span>
                  {codexOAuthStatus.connected && codexOAuthStatus.plan_type && (
                    <span
                      className="inline-flex items-center px-1.5 py-0.5 rounded-(--radius-control) text-(length:--size-chrome) font-medium"
                      style={{ backgroundColor: 'var(--color-success-soft)', color: 'var(--color-success)' }}
                    >
                      {codexOAuthStatus.plan_type}
                    </span>
                  )}
                </div>
                {codexOAuthStatus.connected ? (
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{codexOAuthStatus.email || codexOAuthStatus.account_id}</p>
                ) : (
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('settings.codexDesc', 'Use Codex models with your ChatGPT subscription')}
                  </p>
                )}
              </div>
            </div>
            {/* The description wraps instead: a squeezed CJK label breaks between any two characters. */}
            <div className="shrink-0">
              {codexOAuthStatus.connected ? (
                <button
                  type="button"
                  onClick={handleCodexDisconnect}
                  disabled={isDisconnectingCodex}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-(--radius-control) text-xs font-medium transition-colors"
                  style={{ color: 'var(--color-loss)', backgroundColor: 'transparent', border: '1px solid var(--color-loss)' }}
                >
                  <Unlink className="h-3 w-3" />
                  {isDisconnectingCodex ? t('common.loading', 'Loading...') : t('settings.disconnect', 'Disconnect')}
                </button>
              ) : !codexDeviceCode ? (
                <button
                  type="button"
                  onClick={handleCodexConnectClick}
                  disabled={isConnectingCodex}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-(--radius-control) text-xs font-medium transition-opacity hover:opacity-90"
                  style={{
                    backgroundColor: isConnectingCodex ? 'var(--color-bg-elevated)' : 'var(--color-btn-primary-bg)',
                    color: isConnectingCodex ? 'var(--color-text-tertiary)' : 'var(--color-btn-primary-text)',
                  }}
                >
                  <Link2 className="h-3 w-3" />
                  {isConnectingCodex ? t('common.loading', 'Loading...') : t('settings.connect', 'Connect')}
                </button>
              ) : null}
            </div>
          </div>

          {/* Device code dialog — shown while waiting for user approval */}
          {codexDeviceCode && !codexOAuthStatus.connected && (
            <div className="mt-3 pt-3" style={{ borderTop: '1px solid var(--color-border-muted)' }}>
              <p className="text-xs mb-2" style={{ color: 'var(--color-text-secondary)' }}>
                {t('settings.codexVisit')} <a href={codexDeviceCode.verification_url} target="_blank" rel="noopener noreferrer" className="underline" style={{ color: 'var(--color-accent-primary)' }}>{codexDeviceCode.verification_url}</a> {t('settings.codexEnterCode')}
              </p>
              <div className="flex items-center gap-2 mb-2">
                <code
                  className="text-lg font-mono font-bold tracking-widest px-3 py-1.5 rounded-(--radius-control) select-all"
                  style={{
                    backgroundColor: 'var(--color-bg-elevated)',
                    border: '1px solid var(--color-border-muted)',
                    color: 'var(--color-text-primary)',
                    letterSpacing: '0.15em',
                  }}
                >
                  {codexDeviceCode.user_code}
                </code>
                <button
                  type="button"
                  onClick={() => navigator.clipboard.writeText(codexDeviceCode.user_code)}
                  className="p-1.5 rounded-(--radius-control) transition-colors hover:opacity-80"
                  style={{ backgroundColor: 'var(--color-bg-elevated)', border: '1px solid var(--color-border-muted)' }}
                  title={t('common.copy', 'Copy')}
                >
                  <ClipboardCopy className="h-3.5 w-3.5" style={{ color: 'var(--color-text-tertiary)' }} />
                </button>
                {isPollingCodex && (
                  <span className="text-xs animate-pulse" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('settings.codexWaitingApproval')}
                  </span>
                )}
              </div>
              <button
                type="button"
                onClick={handleCodexDeviceCancel}
                className="px-3 py-1.5 rounded-(--radius-control) text-xs font-medium"
                style={{ color: 'var(--color-text-tertiary)', backgroundColor: 'transparent' }}
              >
                {t('common.cancel', 'Cancel')}
              </button>
              {codexDeviceError && (
                <p className="text-xs mt-1.5" style={{ color: 'var(--color-loss)' }}>{codexDeviceError}</p>
              )}
            </div>
          )}
        </div>

        {/* Claude OAuth card */}
        <div
          className="tn-settings-card rounded-(--radius-card) px-4 py-3 mt-2"
          style={{
            backgroundColor: 'var(--color-bg-card)',
            border: `1px solid ${claudeOAuthStatus.connected ? 'var(--color-success-soft)' : 'var(--color-border-muted)'}`,
          }}
        >
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div
                className="h-8 w-8 rounded-(--radius-control) flex items-center justify-center"
                style={{ backgroundColor: claudeOAuthStatus.connected ? 'var(--color-success-soft)' : 'var(--color-accent-soft)' }}
              >
                <Link2 className="h-4 w-4" style={{ color: claudeOAuthStatus.connected ? 'var(--color-success)' : 'var(--color-accent-primary)' }} />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>Claude Code</span>
                  {claudeOAuthStatus.connected && claudeOAuthStatus.plan_type && (
                    <span
                      className="inline-flex items-center px-1.5 py-0.5 rounded-(--radius-control) text-(length:--size-chrome) font-medium"
                      style={{ backgroundColor: 'var(--color-success-soft)', color: 'var(--color-success)' }}
                    >
                      {claudeOAuthStatus.plan_type}
                    </span>
                  )}
                </div>
                {claudeOAuthStatus.connected ? (
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{claudeOAuthStatus.email || claudeOAuthStatus.account_id || t('settings.connected', 'Connected')}</p>
                ) : (
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('settings.claudeDesc', 'Use Claude models with your Anthropic subscription')}
                  </p>
                )}
              </div>
            </div>
            <div className="shrink-0">
              {claudeOAuthStatus.connected ? (
                <button
                  type="button"
                  onClick={handleClaudeDisconnect}
                  disabled={isDisconnectingClaude}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-(--radius-control) text-xs font-medium transition-colors"
                  style={{ color: 'var(--color-loss)', backgroundColor: 'transparent', border: '1px solid var(--color-loss)' }}
                >
                  <Unlink className="h-3 w-3" />
                  {isDisconnectingClaude ? t('common.loading', 'Loading...') : t('settings.disconnect', 'Disconnect')}
                </button>
              ) : !claudeAuthorizeUrl ? (
                <button
                  type="button"
                  onClick={handleClaudeConnectClick}
                  disabled={isConnectingClaude}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-(--radius-control) text-xs font-medium transition-opacity hover:opacity-90"
                  style={{
                    backgroundColor: isConnectingClaude ? 'var(--color-bg-elevated)' : 'var(--color-btn-primary-bg)',
                    color: isConnectingClaude ? 'var(--color-text-tertiary)' : 'var(--color-btn-primary-text)',
                  }}
                >
                  <Link2 className="h-3 w-3" />
                  {isConnectingClaude ? t('common.loading', 'Loading...') : t('settings.connect', 'Connect')}
                </button>
              ) : null}
            </div>
          </div>

          {/* Paste-back input — shown after user opens authorize URL */}
          {claudeAuthorizeUrl && !claudeOAuthStatus.connected && (
            <div className="mt-3 pt-3" style={{ borderTop: '1px solid var(--color-border-muted)' }}>
              <p className="text-xs mb-2" style={{ color: 'var(--color-text-secondary)' }}>
                {t('settings.claudePastePrompt', 'After authorizing on claude.ai, paste the code shown on the page below:')}
              </p>
              <div className="flex items-center gap-2 mb-2">
                <Input
                  value={claudeCallbackInput}
                  onChange={(e) => setClaudeCallbackInput(e.target.value)}
                  placeholder="code#state"
                  className="flex-1 text-xs font-mono"
                  onKeyDown={(e) => e.key === 'Enter' && handleClaudeCallbackSubmit()}
                />
                <button
                  type="button"
                  onClick={handleClaudeCallbackSubmit}
                  disabled={isSubmittingClaudeCallback || !claudeCallbackInput.trim()}
                  className="px-3 py-1.5 rounded-(--radius-control) text-xs font-medium transition-opacity hover:opacity-90"
                  style={{
                    backgroundColor: isSubmittingClaudeCallback ? 'var(--color-bg-elevated)' : 'var(--color-btn-primary-bg)',
                    color: isSubmittingClaudeCallback ? 'var(--color-text-tertiary)' : 'var(--color-btn-primary-text)',
                  }}
                >
                  {isSubmittingClaudeCallback ? t('common.loading', 'Loading...') : t('common.submit', 'Submit')}
                </button>
              </div>
              <div className="flex items-center gap-3">
                <a
                  href={claudeAuthorizeUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs underline"
                  style={{ color: 'var(--color-accent-primary)' }}
                >
                  {t('settings.claudeOpenAgain', 'Open authorize page again')}
                </a>
                <button
                  type="button"
                  onClick={handleClaudeCancel}
                  className="px-3 py-1.5 rounded-(--radius-control) text-xs font-medium"
                  style={{ color: 'var(--color-text-tertiary)', backgroundColor: 'transparent' }}
                >
                  {t('common.cancel', 'Cancel')}
                </button>
              </div>
              {claudeError && (
                <p className="text-xs mt-1.5" style={{ color: 'var(--color-loss)' }}>{claudeError}</p>
              )}
            </div>
          )}
        </div>

        {/* Grok OAuth card */}
        <div
          className="tn-settings-card rounded-(--radius-card) px-4 py-3 mt-2"
          style={{
            backgroundColor: 'var(--color-bg-card)',
            border: `1px solid ${grokOAuthStatus.connected ? 'var(--color-success-soft)' : 'var(--color-border-muted)'}`,
          }}
        >
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div
                className="h-8 w-8 rounded-(--radius-control) flex items-center justify-center"
                style={{ backgroundColor: grokOAuthStatus.connected ? 'var(--color-success-soft)' : 'var(--color-accent-soft)' }}
              >
                <Link2 className="h-4 w-4" style={{ color: grokOAuthStatus.connected ? 'var(--color-success)' : 'var(--color-accent-primary)' }} />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('tnSettings.providers.grokLogin')}</span>
                  {grokOAuthStatus.connected && grokOAuthStatus.plan_type && (
                    <span className="inline-flex items-center px-1.5 py-0.5 rounded-(--radius-control) text-(length:--size-chrome) font-medium" style={{ backgroundColor: 'var(--color-success-soft)', color: 'var(--color-success)' }}>
                      {grokOAuthStatus.plan_type}
                    </span>
                  )}
                </div>
                {grokOAuthStatus.connected ? (
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{grokOAuthStatus.email || grokOAuthStatus.account_id || t('settings.connected', 'Connected')}</p>
                ) : (
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('settings.grokDesc', 'Use Grok models with your SuperGrok / X Premium subscription')}
                  </p>
                )}
              </div>
            </div>
            <div className="shrink-0">
              {grokOAuthStatus.connected ? (
                <button type="button" onClick={handleGrokDisconnect} disabled={isDisconnectingGrok}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-(--radius-control) text-xs font-medium transition-colors"
                  style={{ color: 'var(--color-loss)', backgroundColor: 'transparent', border: '1px solid var(--color-loss)' }}>
                  <Unlink className="h-3 w-3" />
                  {isDisconnectingGrok ? t('common.loading', 'Loading...') : t('settings.disconnect', 'Disconnect')}
                </button>
              ) : !grokDeviceCode ? (
                <button type="button" onClick={handleGrokConnect} disabled={isConnectingGrok}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-(--radius-control) text-xs font-medium transition-opacity hover:opacity-90"
                  style={{ backgroundColor: isConnectingGrok ? 'var(--color-bg-elevated)' : 'var(--color-btn-primary-bg)', color: isConnectingGrok ? 'var(--color-text-tertiary)' : 'var(--color-btn-primary-text)' }}>
                  <Link2 className="h-3 w-3" />
                  {isConnectingGrok ? t('common.loading', 'Loading...') : t('settings.connect', 'Connect')}
                </button>
              ) : null}
            </div>
          </div>

          {grokDeviceCode && !grokOAuthStatus.connected && (
            <div className="mt-3 pt-3" style={{ borderTop: '1px solid var(--color-border-muted)' }}>
              <p className="text-xs mb-2" style={{ color: 'var(--color-text-secondary)' }}>
                {t('settings.codexVisit')} <a href={grokDeviceCode.verification_url} target="_blank" rel="noopener noreferrer" className="underline" style={{ color: 'var(--color-accent-primary)' }}>{grokDeviceCode.verification_url}</a> {t('settings.codexEnterCode')}
              </p>
              <div className="flex items-center gap-2 mb-2">
                <code className="text-lg font-mono font-bold tracking-widest px-3 py-1.5 rounded-(--radius-control) select-all"
                  style={{ backgroundColor: 'var(--color-bg-elevated)', border: '1px solid var(--color-border-muted)', color: 'var(--color-text-primary)', letterSpacing: '0.15em' }}>
                  {grokDeviceCode.user_code}
                </code>
                <button type="button" onClick={() => navigator.clipboard.writeText(grokDeviceCode.user_code)}
                  className="p-1.5 rounded-(--radius-control) transition-colors hover:opacity-80"
                  style={{ backgroundColor: 'var(--color-bg-elevated)', border: '1px solid var(--color-border-muted)' }} title={t('common.copy', 'Copy')}>
                  <ClipboardCopy className="h-3.5 w-3.5" style={{ color: 'var(--color-text-tertiary)' }} />
                </button>
                {isPollingGrok && (
                  <span className="text-xs animate-pulse" style={{ color: 'var(--color-text-tertiary)' }}>{t('settings.codexWaitingApproval')}</span>
                )}
              </div>
              <button type="button" onClick={handleGrokDeviceCancel}
                className="px-3 py-1.5 rounded-(--radius-control) text-xs font-medium"
                style={{ color: 'var(--color-text-tertiary)', backgroundColor: 'transparent' }}>
                {t('common.cancel', 'Cancel')}
              </button>
              {grokDeviceError && <p className="text-xs mt-1.5" style={{ color: 'var(--color-loss)' }}>{grokDeviceError}</p>}
            </div>
          )}
        </div>

        {/* Gemini OAuth card */}
        <div
          className="tn-settings-card rounded-(--radius-card) px-4 py-3 mt-2"
          style={{
            backgroundColor: 'var(--color-bg-card)',
            border: `1px solid ${geminiOAuthStatus.connected ? 'var(--color-success-soft)' : 'var(--color-border-muted)'}`,
          }}
        >
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div
                className="h-8 w-8 rounded-(--radius-control) flex items-center justify-center"
                style={{ backgroundColor: geminiOAuthStatus.connected ? 'var(--color-success-soft)' : 'var(--color-accent-soft)' }}
              >
                <Link2 className="h-4 w-4" style={{ color: geminiOAuthStatus.connected ? 'var(--color-success)' : 'var(--color-accent-primary)' }} />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('tnSettings.providers.geminiLogin')}</span>
                  {geminiOAuthStatus.connected && geminiOAuthStatus.plan_type && (
                    <span className="inline-flex items-center px-1.5 py-0.5 rounded-(--radius-control) text-(length:--size-chrome) font-medium" style={{ backgroundColor: 'var(--color-success-soft)', color: 'var(--color-success)' }}>
                      {geminiOAuthStatus.plan_type}
                    </span>
                  )}
                </div>
                {geminiOAuthStatus.connected ? (
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{geminiOAuthStatus.email || geminiOAuthStatus.account_id || t('settings.connected', 'Connected')}</p>
                ) : (
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('settings.geminiDesc', 'Use Gemini models with your Google account (Gemini Code Assist)')}
                  </p>
                )}
              </div>
            </div>
            <div className="shrink-0">
              {geminiOAuthStatus.connected ? (
                <button type="button" onClick={handleGeminiDisconnect} disabled={isDisconnectingGemini}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-(--radius-control) text-xs font-medium transition-colors"
                  style={{ color: 'var(--color-loss)', backgroundColor: 'transparent', border: '1px solid var(--color-loss)' }}>
                  <Unlink className="h-3 w-3" />
                  {isDisconnectingGemini ? t('common.loading', 'Loading...') : t('settings.disconnect', 'Disconnect')}
                </button>
              ) : !geminiAuthorizeUrl ? (
                <button type="button" onClick={handleGeminiConnect} disabled={isConnectingGemini}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-(--radius-control) text-xs font-medium transition-opacity hover:opacity-90"
                  style={{ backgroundColor: isConnectingGemini ? 'var(--color-bg-elevated)' : 'var(--color-btn-primary-bg)', color: isConnectingGemini ? 'var(--color-text-tertiary)' : 'var(--color-btn-primary-text)' }}>
                  <Link2 className="h-3 w-3" />
                  {isConnectingGemini ? t('common.loading', 'Loading...') : t('settings.connect', 'Connect')}
                </button>
              ) : null}
            </div>
          </div>

          {!geminiOAuthStatus.connected && (
            <div
              className="mt-3 rounded-(--radius-control) px-3 py-2 text-(length:--size-chrome) leading-relaxed"
              style={{
                backgroundColor: 'var(--color-accent-soft)',
                border: '1px solid var(--color-accent-overlay)',
                color: 'var(--color-text-secondary)',
              }}
              data-testid="gemini-antigravity-notice"
            >
              <p className="font-medium" style={{ color: 'var(--color-accent-primary)' }}>
                {t(
                  'settings.geminiConsumerRetiredTitle',
                  'Sign in with Google through Antigravity',
                )}
              </p>
              <p className="mt-1">
                {t(
                  'settings.geminiConsumerRetiredBody',
                  'Antigravity is Google’s current Gemini developer client. Sign-in uses your Antigravity / Gemini Code Assist entitlement; the retired Gemini CLI consumer OAuth is not used.',
                )}
              </p>
              <div className="mt-1.5 flex flex-wrap gap-3">
                <a
                  href="https://aistudio.google.com/apikey"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="underline"
                  style={{ color: 'var(--color-accent-primary)' }}
                >
                  {t('settings.geminiUseApiKey', 'Get a Gemini API key')}
                </a>
                <a
                  href="https://antigravity.google"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="underline"
                  style={{ color: 'var(--color-accent-primary)' }}
                >
                  {t('settings.geminiMigrateAntigravity', 'Antigravity')}
                </a>
              </div>
            </div>
          )}

          {geminiAuthorizeUrl && !geminiOAuthStatus.connected && (
            <div className="mt-3 pt-3" style={{ borderTop: '1px solid var(--color-border-muted)' }}>
              <p className="text-xs mb-2" style={{ color: 'var(--color-text-secondary)' }}>
                {t('settings.geminiPastePrompt', 'After signing in with Google, paste the authorization code (or the redirect URL) below:')}
              </p>
              <div className="flex items-center gap-2 mb-2">
                <Input value={geminiCallbackInput} onChange={(e) => setGeminiCallbackInput(e.target.value)}
                  placeholder="4/0Ax4Xf..." className="flex-1 text-xs font-mono"
                  onKeyDown={(e) => e.key === 'Enter' && handleGeminiSubmit()} />
                <button type="button" onClick={handleGeminiSubmit} disabled={isSubmittingGemini || !geminiCallbackInput.trim()}
                  className="px-3 py-1.5 rounded-(--radius-control) text-xs font-medium transition-opacity hover:opacity-90"
                  style={{ backgroundColor: isSubmittingGemini ? 'var(--color-bg-elevated)' : 'var(--color-btn-primary-bg)', color: isSubmittingGemini ? 'var(--color-text-tertiary)' : 'var(--color-btn-primary-text)' }}>
                  {isSubmittingGemini ? t('common.loading', 'Loading...') : t('common.submit', 'Submit')}
                </button>
              </div>
              <div className="flex items-center gap-3">
                <a href={geminiAuthorizeUrl} target="_blank" rel="noopener noreferrer" className="text-xs underline" style={{ color: 'var(--color-accent-primary)' }}>
                  {t('settings.geminiOpenAgain', 'Open Google sign-in again')}
                </a>
                <button type="button" onClick={() => { setGeminiAuthorizeUrl(null); setGeminiCallbackInput(''); setGeminiError(null); }}
                  className="px-3 py-1.5 rounded-(--radius-control) text-xs font-medium"
                  style={{ color: 'var(--color-text-tertiary)', backgroundColor: 'transparent' }}>
                  {t('common.cancel', 'Cancel')}
                </button>
              </div>
              {geminiError && <p className="text-xs mt-1.5" style={{ color: 'var(--color-loss)' }}>{geminiError}</p>}
            </div>
          )}
        </div>

        {accountsError && (
          <div className="p-3 rounded-(--radius-control) mt-2" style={{ backgroundColor: 'var(--color-loss-soft)', border: '1px solid var(--color-border-loss)' }}>
            <p className="text-sm" style={{ color: 'var(--color-loss)' }}>{accountsError}</p>
          </div>
        )}
      </div>

      {/* Codex OAuth Disclaimer Dialog */}
      <Dialog open={showCodexDisclaimer} onOpenChange={setShowCodexDisclaimer}>
        <DialogContent
          className="sm:max-w-md border"
          style={{ backgroundColor: 'var(--color-bg-elevated)', borderColor: 'var(--color-border-elevated)' }}
        >
          <DialogHeader>
            <DialogTitle className="title-font flex items-center gap-2" style={{ color: 'var(--color-text-primary)' }}>
              <Link2 className="h-5 w-5" style={{ color: 'var(--color-accent-primary)' }} />
              {t('settings.codexConnectTitle')}
            </DialogTitle>
          </DialogHeader>

          <div className="space-y-4">
            {/* Steps */}
            <div className="space-y-3">
              <p className="text-xs font-medium uppercase tracking-wide" style={{ color: 'var(--color-text-tertiary)' }}>{t('settings.codexHowItWorks')}</p>

              <div className="flex gap-3 items-start">
                <div className="shrink-0 h-6 w-6 rounded-(--radius-control) flex items-center justify-center text-xs font-bold" style={{ backgroundColor: 'var(--color-accent-soft)', color: 'var(--color-accent-primary)' }}>1</div>
                <div>
                  <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('settings.codexStep1Title')}</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{t('settings.codexStep1Desc')}</p>
                </div>
              </div>

              <div className="flex gap-3 items-start">
                <div className="shrink-0 h-6 w-6 rounded-(--radius-control) flex items-center justify-center text-xs font-bold" style={{ backgroundColor: 'var(--color-accent-soft)', color: 'var(--color-accent-primary)' }}>2</div>
                <div>
                  <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('settings.codexStep2Title')}</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{t('settings.codexStep2Desc')}</p>
                </div>
              </div>

              <div className="flex gap-3 items-start">
                <div className="shrink-0 h-6 w-6 rounded-(--radius-control) flex items-center justify-center text-xs font-bold" style={{ backgroundColor: 'var(--color-accent-soft)', color: 'var(--color-accent-primary)' }}>3</div>
                <div>
                  <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('settings.codexStep3Title')}</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{t('settings.codexStep3Desc')}</p>
                </div>
              </div>
            </div>

            {/* Disclaimer */}
            <div className="rounded-(--radius-card) p-3" style={{ backgroundColor: 'var(--color-bg-subtle)', border: '1px solid var(--color-border-muted)' }}>
              <div className="flex gap-2 items-start">
                <Shield className="h-4 w-4 shrink-0 mt-0.5" style={{ color: 'var(--color-text-tertiary)' }} />
                <div>
                  <p className="text-xs font-medium mb-1" style={{ color: 'var(--color-text-secondary)' }}>{t('settings.codexSecurityTitle')}</p>
                  <p className="text-(length:--size-chrome) leading-relaxed" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('settings.codexSecurityDesc')}
                  </p>
                  <p className="text-(length:--size-chrome) leading-relaxed mt-1.5" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('settings.codexDisclaimerDesc')}
                  </p>
                </div>
              </div>
            </div>
          </div>

          <DialogFooter className="gap-2 pt-2">
            <button
              type="button"
              onClick={() => setShowCodexDisclaimer(false)}
              className="px-3 py-1.5 rounded-(--radius-control) text-sm border"
              style={{ color: 'var(--color-text-primary)', borderColor: 'var(--color-border-default)' }}
              onMouseEnter={(e) => e.currentTarget.style.backgroundColor = 'var(--color-border-muted)'}
              onMouseLeave={(e) => e.currentTarget.style.backgroundColor = 'transparent'}
            >
              {t('common.cancel', 'Cancel')}
            </button>
            <button
              type="button"
              onClick={handleCodexConnect}
              className="px-4 py-1.5 rounded-(--radius-control) text-sm font-medium transition-opacity hover:opacity-90 flex items-center gap-1.5"
              style={{ backgroundColor: 'var(--color-btn-primary-bg)', color: 'var(--color-btn-primary-text)' }}
            >
              <ExternalLink className="h-3.5 w-3.5" />
              {t('settings.codexProceed')}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Claude OAuth Disclaimer Dialog */}
      <Dialog open={showClaudeDisclaimer} onOpenChange={setShowClaudeDisclaimer}>
        <DialogContent
          className="sm:max-w-md border"
          style={{ backgroundColor: 'var(--color-bg-elevated)', borderColor: 'var(--color-border-elevated)' }}
        >
          <DialogHeader>
            <DialogTitle className="title-font flex items-center gap-2" style={{ color: 'var(--color-text-primary)' }}>
              <Link2 className="h-5 w-5" style={{ color: 'var(--color-accent-primary)' }} />
              {t('settings.claudeConnectTitle', 'Connect Claude Account')}
            </DialogTitle>
          </DialogHeader>

          <div className="space-y-4">
            {/* Steps */}
            <div className="space-y-3">
              <p className="text-xs font-medium uppercase tracking-wide" style={{ color: 'var(--color-text-tertiary)' }}>{t('settings.claudeHowItWorks', 'How it works')}</p>

              <div className="flex gap-3 items-start">
                <div className="shrink-0 h-6 w-6 rounded-(--radius-control) flex items-center justify-center text-xs font-bold" style={{ backgroundColor: 'var(--color-accent-soft)', color: 'var(--color-accent-primary)' }}>1</div>
                <div>
                  <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('settings.claudeStep1Title', 'Authorize on claude.ai')}</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{t('settings.claudeStep1Desc', 'A new tab will open to claude.ai where you sign in and authorize access.')}</p>
                </div>
              </div>

              <div className="flex gap-3 items-start">
                <div className="shrink-0 h-6 w-6 rounded-(--radius-control) flex items-center justify-center text-xs font-bold" style={{ backgroundColor: 'var(--color-accent-soft)', color: 'var(--color-accent-primary)' }}>2</div>
                <div>
                  <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('settings.claudeStep2Title', 'Copy the authorization code')}</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{t('settings.claudeStep2Desc', 'After approval, you\'ll see a code on the page. Copy the entire value.')}</p>
                </div>
              </div>

              <div className="flex gap-3 items-start">
                <div className="shrink-0 h-6 w-6 rounded-(--radius-control) flex items-center justify-center text-xs font-bold" style={{ backgroundColor: 'var(--color-accent-soft)', color: 'var(--color-accent-primary)' }}>3</div>
                <div>
                  <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{t('settings.claudeStep3Title', 'Paste it back here')}</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>{t('settings.claudeStep3Desc', 'Paste the code into the input field to complete the connection.')}</p>
                </div>
              </div>
            </div>

            {/* Disclaimer */}
            <div className="rounded-(--radius-card) p-3" style={{ backgroundColor: 'var(--color-bg-subtle)', border: '1px solid var(--color-border-muted)' }}>
              <div className="flex gap-2 items-start">
                <Shield className="h-4 w-4 shrink-0 mt-0.5" style={{ color: 'var(--color-text-tertiary)' }} />
                <div>
                  <p className="text-xs font-medium mb-1" style={{ color: 'var(--color-text-secondary)' }}>{t('settings.claudeSecurityTitle', 'Security & Privacy')}</p>
                  <p className="text-(length:--size-chrome) leading-relaxed" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('settings.claudeSecurityDesc', 'Your tokens are encrypted at rest. We use them only to make API calls on your behalf.')}
                  </p>
                  <p className="text-(length:--size-chrome) leading-relaxed mt-1.5" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('settings.claudeDisclaimerDesc', 'Usage will count against your Anthropic subscription. You can disconnect at any time.')}
                  </p>
                </div>
              </div>
            </div>
          </div>

          <DialogFooter className="gap-2 pt-2">
            <button
              type="button"
              onClick={() => setShowClaudeDisclaimer(false)}
              className="px-3 py-1.5 rounded-(--radius-control) text-sm border"
              style={{ color: 'var(--color-text-primary)', borderColor: 'var(--color-border-default)' }}
              onMouseEnter={(e) => e.currentTarget.style.backgroundColor = 'var(--color-border-muted)'}
              onMouseLeave={(e) => e.currentTarget.style.backgroundColor = 'transparent'}
            >
              {t('common.cancel', 'Cancel')}
            </button>
            <button
              type="button"
              onClick={handleClaudeConnect}
              className="px-4 py-1.5 rounded-(--radius-control) text-sm font-medium transition-opacity hover:opacity-90 flex items-center gap-1.5"
              style={{ backgroundColor: 'var(--color-btn-primary-bg)', color: 'var(--color-btn-primary-text)' }}
            >
              <ExternalLink className="h-3.5 w-3.5" />
              {t('settings.claudeProceed', 'Open claude.ai')}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
