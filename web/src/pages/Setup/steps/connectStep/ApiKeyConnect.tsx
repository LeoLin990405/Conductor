import { useState, useCallback, useId } from 'react';
import { useNavigate } from 'react-router';
import { useQueryClient } from '@tanstack/react-query';
import { Link2 } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { WizardStepHeader } from '@/components/ui/wizard';
import { BrandMark } from '@/components/mcp/BrandMark';
import { llmProviderArt } from '@/lib/brandArt';
import { ApiKeyInput, type TestResult } from '@/components/model/ApiKeyInput';
import { SetupFooter } from '../../SetupFooter';
import { useUpdateApiKeys } from '@/hooks/useApiKeys';
import { useLocale } from '@/hooks/useLocale';
import { useOAuthProviders } from '@/hooks/useOAuthProviders';
import { queryKeys } from '@/lib/queryKeys';
import { useTranslation } from 'react-i18next';
import { useToast } from '@/components/ui/use-toast';
import { getApiFormatKey, testApiKey, useProviderConnected, type LocationState } from './shared';

export function ApiKeyConnect({ state }: { state: LocationState }) {
  const navigate = useNavigate();
  const connected = useProviderConnected();
  const queryClient = useQueryClient();
  const updateApiKeys = useUpdateApiKeys();
  const { t } = useTranslation();
  const { toast } = useToast();
  const headingId = useId();
  const regionLabelId = useId();
  const baseUrlId = useId();
  const apiKeyId = useId();

  const method = state.method ?? 'api_key';
  const provider = state.provider ?? '';
  const displayName = state.displayName ?? provider;
  const brandKey = state.brandKey ?? provider;
  const sdk = state.sdk ?? null;
  const defaultBaseUrl = state.defaultBaseUrl ?? null;
  const useResponseApi = state.useResponseApi ?? false;
  const regionVariants = state.regionVariants ?? null;
  const defaultRegion = state.defaultRegion ?? null;
  const dynamicModels = state.dynamicModels ?? false;
  const apiFormatKey = getApiFormatKey(sdk, useResponseApi);
  const requestedPlanKind = state.planKind ?? null;
  const requestedPlanName = state.planName ?? null;
  const accessType = state.accessType ?? method;
  const isPlan = accessType === 'coding_plan';
  const planLabel =
    requestedPlanName ||
    (requestedPlanKind === 'token'
      ? t('setup.accessTokenPlan')
      : t('setup.accessAgentPlan'));
  const planKindLabel =
    requestedPlanKind === 'token'
      ? t('setup.accessTokenPlan')
      : t('setup.accessAgentPlan');

  // Region selection state — when variants exist, user can switch
  const [selectedRegion, setSelectedRegion] = useState<string | null>(null);

  // Compute effective provider/base_url/sdk based on region selection
  const activeVariant = selectedRegion && regionVariants
    ? regionVariants.find((v) => v.region === selectedRegion)
    : null;
  const effectiveProvider = activeVariant?.provider ?? provider;
  const effectiveBaseUrl = activeVariant?.base_url ?? defaultBaseUrl ?? '';
  const effectiveSdk = activeVariant?.sdk ?? sdk;
  const effectiveUseResponseApi = activeVariant?.use_response_api ?? useResponseApi;
  const effectiveApiFormatKey = activeVariant
    ? getApiFormatKey(effectiveSdk, effectiveUseResponseApi)
    : apiFormatKey;

  // The line the success notice and the Settings summary both need: "API
  // China", "M Plan · China", "Step Plan · International". The provider id
  // alone (minimax-coding vs minimax-intl) is exactly what users could not
  // see after saving.
  const configuredRegion = selectedRegion ?? defaultRegion;
  const configuredRegionLabel = configuredRegion === 'intl'
    ? t('setup.regionInternational', 'International')
    : configuredRegion === 'cn'
      ? t('setup.regionChina', 'China')
      : '';
  const configuredRoute = isPlan
    ? [planLabel, configuredRegionLabel].filter(Boolean).join(' · ')
    : ['API', configuredRegionLabel].filter(Boolean).join(' ');


  // The plan's own subscription login, from the backend capability catalog: a
  // descriptor naming this provider as its API-key fallback is the same plan's
  // sign-in. The API key stays this step's primary action.
  const { providers: connectionDescriptors } = useOAuthProviders();
  const locale = useLocale();
  const accountLogin = connectionDescriptors.find(
    (descriptor) =>
      descriptor.login_supported &&
      descriptor.kind !== 'api_key_only' &&
      descriptor.api_key_provider !== null &&
      descriptor.api_key_provider === (effectiveProvider || provider),
  ) ?? null;
  const accountLoginName = accountLogin
    ? (locale.toLowerCase().startsWith('zh')
        ? accountLogin.display_name
        : accountLogin.display_name_en) || accountLogin.display_name || accountLogin.id
    : null;
  // Some Chinese catalog descriptors already end in 账号登录; the locale string
  // supplies that phrase, so strip it here rather than render it twice.
  const accountLoginLabel = locale.toLowerCase().startsWith('zh')
    ? accountLoginName?.replace(/\s*账号登录$/, '')
    : accountLoginName;

  const handleRegionChange = useCallback((region: string | null) => {
    setSelectedRegion(region);
    if (region && regionVariants) {
      const v = regionVariants.find((rv) => rv.region === region);
      if (v?.base_url) setBaseUrl(v.base_url);
    } else {
      setBaseUrl(defaultBaseUrl ?? '');
    }
  }, [regionVariants, defaultBaseUrl]);

  // API key / coding plan state
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState(defaultBaseUrl ?? '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ---------------------------------------------------------------------------
  // API key / coding plan handlers
  // ---------------------------------------------------------------------------

  const handleTestKey = useCallback(
    async (_provider: string, key: string): Promise<TestResult> => {
      return testApiKey(effectiveProvider || brandKey, key, baseUrl || undefined);
    },
    [effectiveProvider, brandKey, baseUrl],
  );

  const handleSaveAndNext = useCallback(async () => {
    if (!dynamicModels && !apiKey.trim()) {
      setError(t('setup.errorNoApiKey'));
      return;
    }
    if (dynamicModels && !baseUrl.trim()) {
      setError(t('setup.errorNoBaseUrl'));
      return;
    }

    setSaving(true);
    setError(null);

    try {
      const saveProvider = effectiveProvider || provider;
      const payload: Record<string, unknown> = {
        byok_enabled: true,
        api_keys: { [saveProvider]: apiKey },
      };
      if (baseUrl.trim()) {
        payload.base_urls = { [saveProvider]: baseUrl };
      }
      await updateApiKeys.mutateAsync(payload);

      toast({
        title: t('setup.keySavedTitle', { provider: displayName }),
        description: t('setup.keySavedBody', { route: configuredRoute }),
      });

      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.user.me() }),
        queryClient.invalidateQueries({ queryKey: queryKeys.user.apiKeys() }),
      ]);

      if (dynamicModels) {
        // Dynamic providers (LM Studio, vLLM, Ollama) — go to model discovery.
        // A local server has no models until some are picked from it, so its
        // return waits for that step.
        navigate('/setup/connect', {
          state: {
            method,
            provider: saveProvider,
            displayName,
            brandKey,
            isExistingCustom: true,
            dynamicModels: true,
          },
        });
      } else {
        connected({ method, provider: saveProvider, displayName, brandKey });
      }
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      const detail = err?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : err?.message ?? t('setup.errorSaveKey'));
      setSaving(false);
    }
  }, [apiKey, baseUrl, dynamicModels, effectiveProvider, provider, updateApiKeys, queryClient, navigate, connected, method, displayName, brandKey, t, toast, configuredRoute]);

  const handleBack = useCallback(() => {
    navigate('/setup/provider', { state: { method } });
  }, [navigate, method]);

  return (
    <div>
      <WizardStepHeader
        titleId={headingId}
        icon={
          <BrandMark
            name={displayName}
            art={llmProviderArt(effectiveProvider || brandKey)}
            size="lg"
          />
        }
        title={t('setup.connectTitle', { provider: displayName })}
        helper={
          dynamicModels
            ? t('setup.localServerDesc')
            : isPlan
              ? t('setup.planConnectDesc', { provider: displayName, plan: planLabel })
              : t('setup.apiKeyInputDesc')
        }
      />
      <div className="flex flex-col gap-4 sm:gap-6">
      {isPlan && (
        <div
          className="flex flex-wrap items-center gap-2 text-xs"
          data-testid="connect-plan-context"
        >
          <span
            className="inline-flex items-center rounded-md px-2 py-0.5 font-medium"
            style={{
              background: 'var(--color-accent-soft)',
              color: 'var(--color-accent-primary)',
              border: '1px solid var(--color-accent-overlay)',
            }}
          >
            {planLabel}
          </span>
          <span style={{ color: 'var(--color-text-tertiary)' }}>{planKindLabel}</span>
          <span style={{ color: 'var(--color-text-tertiary)' }}>
            {regionVariants && regionVariants.length > 0
              ? t('setup.planKeyHint', { plan: planLabel })
              : t('setup.planRegionFixed', {
                  region:
                    defaultRegion === 'cn'
                      ? t('setup.regionChina')
                      : defaultRegion === 'sg'
                        ? t('setup.regionSingapore')
                        : t('setup.regionInternational'),
                })}
          </span>
        </div>
      )}
      {/* Region toggle — shown when provider has region variants */}
      {regionVariants && regionVariants.length > 0 && (
        <div className="flex items-center gap-2">
          <span
            id={regionLabelId}
            className="text-xs font-medium"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            {t('setup.regionLabel')}
          </span>
          <div
            role="group"
            aria-labelledby={regionLabelId}
            className="inline-flex rounded-md overflow-hidden clips-focus-ring"
            style={{ border: '1px solid var(--color-border-default)' }}
          >
            {/* Default region option */}
            <button
              type="button"
              aria-pressed={!selectedRegion}
              onClick={() => handleRegionChange(null)}
              className="px-3 py-1 text-xs font-medium transition-colors"
              style={{
                background: !selectedRegion ? 'var(--color-btn-primary-bg)' : 'var(--color-bg-surface)',
                color: !selectedRegion ? 'var(--color-btn-primary-text)' : 'var(--color-text-secondary)',
              }}
            >
              {(defaultRegion === 'cn' ? t('setup.regionChina') : defaultRegion === 'sg' ? t('setup.regionSingapore') : t('setup.regionInternational'))}
            </button>
            {regionVariants.map((rv) => (
              <button
                key={rv.provider}
                type="button"
                aria-pressed={selectedRegion === rv.region}
                onClick={() => handleRegionChange(rv.region)}
                className="px-3 py-1 text-xs font-medium transition-colors"
                style={{
                  background: selectedRegion === rv.region ? 'var(--color-btn-primary-bg)' : 'var(--color-bg-surface)',
                  color: selectedRegion === rv.region ? 'var(--color-btn-primary-text)' : 'var(--color-text-secondary)',
                  borderLeft: '1px solid var(--color-border-default)',
                }}
              >
                {rv.region === 'cn' ? t('setup.regionChina') : rv.region === 'sg' ? t('setup.regionSingapore') : rv.region === 'intl' ? t('setup.regionInternational') : rv.region}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Provider info: SDK format + base URL */}
      <div
        className="rounded-lg p-4 flex flex-col gap-3"
        style={{
          background: 'var(--color-bg-surface)',
          border: '1px solid var(--color-border-default)',
        }}
      >
        {/* SDK format badge */}
        <div className="flex items-center gap-2">
          <span
            className="text-xs font-medium"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            {t('setup.apiFormatLabel')}
          </span>
          <span
            className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium"
            style={{
              background: 'var(--color-bg-tag)',
              color: 'var(--color-text-secondary)',
            }}
          >
            {t(effectiveApiFormatKey, { sdk: effectiveSdk })}
          </span>
        </div>

        {/* Base URL — always shown, always editable */}
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between">
            <label
              htmlFor={baseUrlId}
              className="text-xs font-medium"
              style={{ color: 'var(--color-text-tertiary)' }}
            >
              {t('setup.baseUrlLabel')}
            </label>
            {baseUrl !== effectiveBaseUrl && effectiveBaseUrl && (
              <button
                type="button"
                onClick={() => setBaseUrl(effectiveBaseUrl)}
                className="text-[0.6875rem] text-(--color-text-primary) underline underline-offset-2 hover:opacity-80"
              >
                {t('setup.resetToDefault')}
              </button>
            )}
          </div>
          <Input
            id={baseUrlId}
            type="url"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder={effectiveBaseUrl || 'https://...'}
            className="font-mono text-xs"
          />
          {effectiveBaseUrl && baseUrl !== effectiveBaseUrl && baseUrl.trim() !== '' && (
            <p className="text-[0.6875rem]" style={{ color: 'var(--color-warning, #f59e0b)' }}>
              {t('setup.customUrlWarning', { url: effectiveBaseUrl })}
            </p>
          )}
        </div>
      </div>

      {/* API key input */}
      <div className="flex flex-col gap-3">
        <label
          htmlFor={apiKeyId}
          className="block text-sm font-medium"
          style={{ color: 'var(--color-text-primary)' }}
        >
          {isPlan
            ? t('setup.planKeyLabel', { plan: planLabel })
            : t('setup.providerApiKey', { provider: displayName })}
          {dynamicModels && (
            <span className="text-xs font-normal ml-1.5" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('setup.optional')}
            </span>
          )}
        </label>
        <ApiKeyInput
          id={apiKeyId}
          provider={provider}
          value={apiKey}
          onChange={setApiKey}
          onTest={dynamicModels ? undefined : handleTestKey}
        />
        {isPlan && (
          <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('setup.planKeyHint', { plan: planLabel })}
          </p>
        )}
      </div>

      {/* The plan's subscription sign-in is the second choice: the API key
          stays this step's primary action and this only hands the user to the
          OAuth flow for the same model quota. */}
      {accountLogin && accountLoginName && (
        <button
          type="button"
          onClick={() =>
            navigate('/setup/connect', {
              state: {
                method: 'oauth',
                provider: accountLogin.id,
                displayName: accountLoginName,
                brandKey: accountLogin.brand,
              },
            })
          }
          className="flex items-center gap-2 rounded-lg px-4 py-3 text-left transition-opacity hover:opacity-90 w-full"
          style={{
            background: 'var(--color-bg-surface)',
            border: '1px dashed var(--color-border-default)',
          }}
        >
          <Link2 className="h-4 w-4 shrink-0" style={{ color: 'var(--color-accent-primary)' }} />
          <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {t('setup.useProviderAccount', { provider: accountLoginLabel })}
          </span>
        </button>
      )}

      {error && (
        <p role="alert" className="text-sm" style={{ color: 'var(--color-loss)' }}>
          {error}
        </p>
      )}
      </div>

      <SetupFooter
        back={{ label: t('setup.back'), onClick: handleBack }}
        primary={{
          label: saving ? t('setup.saving') : t('setup.continue'),
          onClick: handleSaveAndNext,
          disabled: (!dynamicModels && !apiKey.trim()) || (dynamicModels && !baseUrl.trim()),
          busy: saving,
        }}
      />
    </div>
  );
}
