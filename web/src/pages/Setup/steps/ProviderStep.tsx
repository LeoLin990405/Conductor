import { useState, useCallback, useId, useMemo } from 'react';
import { useNavigate, useLocation } from 'react-router';
import { Plus, Check } from 'lucide-react';
import { Loader } from '@/components/ui/loader';
import { WizardStepHeader } from '@/components/ui/wizard';
import { SetupFooter } from '../SetupFooter';
import { BrandMark } from '@/components/mcp/BrandMark';
import { llmProviderArt } from '@/lib/brandArt';
import { useModels } from '@/hooks/useModels';
import { usePreferences } from '@/hooks/usePreferences';
import { useConfiguredProviders } from '@/hooks/useConfiguredProviders';
import type {
  AccessType,
  PlanKind,
  ProviderCatalogEntry,
  RegionVariant,
} from '@/components/model/types';
import { useTranslation } from 'react-i18next';
import { modelPrefs } from '@/lib/modelPreferences';
import { useProviderConnected } from './connectStep/shared';
import SetupInvitation from './SetupInvitation';

const CUSTOM_PROVIDER_KEY = '__custom__';

type RegionGroup = 'cn' | 'intl' | 'local';

/** The family a catalog row belongs to. Root entries use their brand key;
 *  legacy variants keep their parent's brand, which may itself be a region
 *  variant ("z-ai-cn-coding" arrives with brand_key "z-ai-cn"). Fold those back
 *  onto the family the user actually knows. */
const FAMILY: Record<string, string> = {
  'z-ai-cn': 'z-ai',
  'z-ai-cn-coding': 'z-ai',
  'z-ai-cn-oauth': 'z-ai',
  'z-ai-coding': 'z-ai',
  'z-ai-oauth': 'z-ai',
  'moonshot-coding': 'moonshot',
  'kimi-oauth': 'moonshot',
  'deepseek-account': 'deepseek',
  'dashscope-intl': 'dashscope',
  'minimax-intl': 'minimax',
  'minimax-coding': 'minimax',
  'mimo-oauth': 'mimo',
  'stepfun-plan': 'stepfun',
  'doubao-plan': 'volcengine',
  'doubao-coding': 'volcengine',
  'gemini-oauth': 'gemini',
  'grok-oauth': 'xai',
  'claude-oauth': 'anthropic',
  'codex-oauth': 'openai',
};

const CN_FAMILIES = new Set([
  'deepseek',
  'z-ai',
  'moonshot',
  'dashscope',
  'minimax',
  'mimo',
  'stepfun',
  'volcengine',
]);

const FAMILY_DISPLAY: Record<string, string> = {
  'z-ai': 'Zhipu AI / Z.ai',
  moonshot: 'Moonshot / Kimi',
  deepseek: 'DeepSeek',
  dashscope: 'Qwen / DashScope',
  minimax: 'MiniMax',
  mimo: 'Xiaomi MiMo',
  stepfun: 'StepFun',
  volcengine: 'Doubao / Volcengine',
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  gemini: 'Google Gemini',
  xai: 'xAI / Grok',
  openrouter: 'OpenRouter',
  groq: 'Groq',
  cerebras: 'Cerebras',
  deepinfra: 'DeepInfra',
  ollama: 'Ollama',
  'lm-studio': 'LM Studio',
  vllm: 'vLLM',
};

interface AccessOption {
  /** Unique selection key (provider id + region). */
  key: string;
  /** Provider id the connect step's API key / subscription save uses. */
  provider: string;
  /** Provider id to display in the configured set. */
  configuredProvider: string;
  label: string;
  access: AccessType;
  region: string;
  entry: ProviderCatalogEntry;
  variant?: RegionVariant;
  /** Token vs Agent plan, from the product capability matrix. */
  planKind?: PlanKind;
  planName?: string;
}

interface BrandGroup {
  family: string;
  displayName: string;
  region: RegionGroup;
  options: AccessOption[];
}

const ACCESS_KEYS: Record<AccessType, string> = {
  api_key: 'setup.accessApi',
  coding_plan: 'setup.accessPlan',
  oauth: 'setup.accessSubscriptionAgent',
  local: 'setup.accessLocal',
};

/** The chip label for a route: a named Token/Agent plan reads as such, not
 *  as the generic "Plan" the wire access type used to print. */
function accessLabelKey(option: AccessOption): string {
  if (option.access === 'oauth') {
    // Only a family with an Agent-plan capability calls its subscription an
    // Agent subscription; DeepSeek's account login stays a plain subscription.
    return option.entry.plan_capabilities?.agent_plan
      ? 'setup.accessSubscriptionAgent'
      : 'setup.accessSubscription';
  }
  if (option.access === 'coding_plan') {
    if (option.planKind === 'token') return 'setup.accessTokenPlan';
    if (option.planKind === 'agent') return 'setup.accessAgentPlan';
  }
  return ACCESS_KEYS[option.access];
}

function familyOf(entry: ProviderCatalogEntry): string {
  const brand = entry.brand_key || entry.provider;
  return FAMILY[brand] ?? FAMILY[entry.provider] ?? brand;
}

function regionOf(entry: ProviderCatalogEntry): string {
  return entry.region === 'cn' ? 'cn' : entry.region === 'local' ? 'local' : 'intl';
}

/** One card per model family, with every way it can be configured. */
function buildGroups(catalog: ProviderCatalogEntry[]): BrandGroup[] {
  const groups = new Map<string, BrandGroup>();

  const add = (
    family: string,
    displayName: string,
    entry: ProviderCatalogEntry,
    variant?: RegionVariant,
  ) => {
    const access = entry.access_type;
    const region = variant?.region ?? entry.region ?? 'intl';
    const isLocal = access === 'local';
    const groupRegion: RegionGroup = isLocal
      ? 'local'
      : CN_FAMILIES.has(family)
        ? 'cn'
        : 'intl';
    let group = groups.get(family);
    if (!group) {
      group = {
        family,
        displayName: FAMILY_DISPLAY[family] ?? displayName,
        region: groupRegion,
        options: [],
      };
      groups.set(family, group);
    }
    const provider = entry.provider;
    const configuredProvider = variant?.provider ?? provider;
    const key = `${provider}|${region}|${access}`;
    if (group.options.some((o) => o.key === key)) return;
    group.options.push({
      key,
      provider,
      configuredProvider,
      label: ACCESS_KEYS[access],
      access,
      region: isLocal ? 'local' : regionOf({ ...entry, region } as ProviderCatalogEntry),
      entry,
      variant,
      planKind: entry.plan_kind,
      planName: entry.plan_name,
    });
  };

  for (const entry of catalog) {
    if (entry.deprecated) continue;
    const family = familyOf(entry);
    add(family, entry.display_name, entry);
    for (const variant of entry.region_variants ?? []) {
      add(family, entry.display_name, entry, variant);
    }
  }

  // API first, then subscription plans, then subscription logins; within a
  // kind, domestic before overseas so the common path is left-most.
  const accessRank: Record<AccessType, number> = {
    api_key: 0,
    coding_plan: 1,
    oauth: 2,
    local: 3,
  };
  for (const group of groups.values()) {
    group.options.sort(
      (a, b) =>
        accessRank[a.access] - accessRank[b.access] ||
        (a.region === b.region ? 0 : a.region === 'cn' ? -1 : 1),
    );
  }
  return [...groups.values()];
}

const REGION_ORDER: RegionGroup[] = ['cn', 'intl', 'local'];
const REGION_KEYS: Record<RegionGroup, string> = {
  cn: 'setup.regionGroupChina',
  intl: 'setup.regionGroupAmerica',
  local: 'setup.regionGroupLocal',
};

// ---------------------------------------------------------------------------
// ProviderStep — Step 2: choose a model family, then how to access it.
// Grouped by where the model lives (China / America+overseas / local) instead
// of by access method, so one brand's API key, coding plan and subscription
// are visible together, with domestic/overseas variants side by side.
// ---------------------------------------------------------------------------

export default function ProviderStep() {
  const navigate = useNavigate();
  const connected = useProviderConnected();
  const location = useLocation();

  const method = (location.state as { method?: AccessType } | null)?.method ?? 'api_key';

  const { models: modelsData, isLoading } = useModels();
  const { preferences } = usePreferences();
  const { configuredSet } = useConfiguredProviders();
  const { t } = useTranslation();
  const [selected, setSelected] = useState<string | null>(null);
  const [selectedOption, setSelectedOption] = useState<string | null>(null);
  const headingId = useId();

  const catalog = useMemo<ProviderCatalogEntry[]>(() => {
    if (!modelsData) return [];
    const raw = modelsData as Record<string, unknown>;
    return (raw.provider_catalog as ProviderCatalogEntry[] | undefined) ?? [];
  }, [modelsData]);

  const groups = useMemo(() => buildGroups(catalog), [catalog]);
  const groupsByRegion = useMemo(() => {
    const byRegion = new Map<RegionGroup, BrandGroup[]>();
    for (const group of groups) {
      const list = byRegion.get(group.region) ?? [];
      list.push(group);
      byRegion.set(group.region, list);
    }
    return byRegion;
  }, [groups]);

  const userCustomProviders = useMemo(() => {
    if (!preferences) return [];
    const cp = modelPrefs(preferences).custom_providers;
    return Array.isArray(cp) ? cp : [];
  }, [preferences]);

  const customProviderSet = useMemo(
    () => new Set(userCustomProviders.map((p) => p.name)),
    [userCustomProviders],
  );

  const chooseGroup = useCallback(
    (group: BrandGroup) => {
      setSelected(group.family);
      const preferred =
        group.options.find((o) => o.access === method) ?? group.options[0];
      setSelectedOption((current) => {
        const currentOption = group.options.find((o) => o.key === current);
        return currentOption?.key ?? preferred?.key ?? null;
      });
    },
    [method],
  );

  const chooseOption = useCallback((group: BrandGroup, option: AccessOption) => {
    setSelected(group.family);
    setSelectedOption(option.key);
  }, []);

  const chosen = useMemo(() => {
    if (!selected || selected === CUSTOM_PROVIDER_KEY) return null;
    const group = groups.find((g) => g.family === selected);
    return group?.options.find((o) => o.key === selectedOption) ?? null;
  }, [groups, selected, selectedOption]);

  const isCustomSelected = selected === CUSTOM_PROVIDER_KEY || (selected !== null && customProviderSet.has(selected));

  const handleNext = useCallback(() => {
    if (!selected) return;

    if (selected === CUSTOM_PROVIDER_KEY) {
      navigate('/setup/connect', { state: { method, isCustom: true } });
      return;
    }

    if (customProviderSet.has(selected)) {
      const customState = {
        method,
        provider: selected,
        displayName: selected,
        brandKey: selected,
      };
      if (configuredSet.has(selected)) connected(customState);
      else navigate('/setup/models', { state: customState });
      return;
    }

    if (!chosen) return;
    const { entry, variant } = chosen;
    const providerState = {
      method: chosen.access,
      provider: chosen.provider,
      displayName: variant?.display_name ?? entry.display_name,
      brandKey: entry.brand_key || entry.provider,
      sdk: variant?.sdk ?? entry.sdk ?? null,
      defaultBaseUrl: variant?.base_url ?? entry.base_url ?? null,
      useResponseApi: variant?.use_response_api ?? entry.use_response_api ?? false,
      regionVariants: entry.region_variants ?? null,
      defaultRegion: chosen.region,
      dynamicModels: entry.dynamic_models ?? false,
      planKind: chosen.planKind,
      planName: chosen.planName,
      accessType: chosen.access,
    };

    if (configuredSet.has(chosen.configuredProvider) || configuredSet.has(entry.provider)) {
      connected(providerState);
      return;
    }
    navigate('/setup/connect', { state: providerState });
  }, [
    selected,
    chosen,
    method,
    customProviderSet,
    configuredSet,
    navigate,
    connected,
  ]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader size={20} className="text-(--color-text-tertiary)" label={t('common.loading')} />
      </div>
    );
  }

  return (
    <div>
      <WizardStepHeader
        titleId={headingId}
        title={t('setup.providerTitle')}
        helper={t('setup.providerGroupedHint')}
      />

      <div className="flex flex-col gap-6">
        {REGION_ORDER.map((region) => {
          const regionGroups = groupsByRegion.get(region);
          if (!regionGroups?.length) return null;
          return (
            <section
              key={region}
              aria-labelledby={`${headingId}-${region}`}
              className="flex flex-col gap-3"
            >
              <div className="flex items-baseline gap-2">
                <h2
                  id={`${headingId}-${region}`}
                  className="text-sm font-semibold"
                  style={{ color: 'var(--color-text-primary)' }}
                >
                  {t(REGION_KEYS[region])}
                </h2>
                <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                  {regionGroups.length}
                </span>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3" role="radiogroup" aria-labelledby={`${headingId}-${region}`}>
                {regionGroups.map((group) => {
                  const isSelected = selected === group.family;
                  const configured = group.options.some(
                    (o) =>
                      configuredSet.has(o.configuredProvider) || configuredSet.has(o.provider),
                  );
                  const familyCaps = group.options[0]?.entry.plan_capabilities;
                  const documentedApiRegions = group.options[0]?.entry.api_regions ?? [];
                  const apiOptions = group.options.filter((o) => o.access === 'api_key');
                  const apiRegions = new Set(apiOptions.map((o) => o.region));
                  const representedRegions = new Set(group.options.map((o) => o.region));
                  const missingApiRegions =
                    apiOptions.length > 0
                      ? documentedApiRegions.filter(
                          (region) =>
                            (region === 'cn' || region === 'intl') &&
                            !apiRegions.has(region),
                        )
                      : [];
                  const missingRegions = documentedApiRegions.filter(
                    (region) =>
                      (region === 'cn' || region === 'intl') &&
                      !representedRegions.has(region) &&
                      !missingApiRegions.includes(region),
                  );
                  const hasTokenOption = group.options.some((o) => o.planKind === 'token');
                  const hasAgentOption = group.options.some((o) => o.planKind === 'agent');
                  const hasOauthOption = group.options.some((o) => o.access === 'oauth');
                  const showRegion =
                    apiRegions.size > 1 ||
                    documentedApiRegions.length > 1 ||
                    group.options.some((o) => o.region !== group.options[0]?.region);
                  const labelFor = (option: AccessOption) => t(accessLabelKey(option));
                  return (
                    <div
                      key={group.family}
                      role="radio"
                      aria-checked={isSelected}
                      aria-label={group.displayName}
                      tabIndex={0}
                      onClick={() => chooseGroup(group)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault();
                          chooseGroup(group);
                        }
                      }}
                      className="relative flex flex-col gap-2 rounded-lg p-3 text-left transition-colors cursor-pointer focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                      style={{
                        border: `1px solid ${isSelected ? 'var(--color-text-primary)' : 'var(--color-border-default)'}`,
                        background: isSelected ? 'var(--color-bg-tag)' : undefined,
                      }}
                    >
                      {configured && (
                        <span
                          className="absolute top-2 right-2 flex h-4 w-4 items-center justify-center rounded-full"
                          style={{ background: 'var(--color-success)' }}
                          aria-label={t('commonUi.apiKeyConfigured')}
                        >
                          <Check className="h-2.5 w-2.5" style={{ color: '#fff' }} strokeWidth={3} />
                        </span>
                      )}
                      <div className="flex items-center gap-2 min-w-0">
                        <BrandMark
                          name={group.displayName}
                          art={llmProviderArt(group.family)}
                          size="sm"
                        />
                        <span
                          className="truncate text-sm font-medium"
                          style={{ color: 'var(--color-text-primary)' }}
                        >
                          {group.displayName}
                        </span>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {group.options.map((option) => {
                          const active = isSelected && selectedOption === option.key;
                          const baseLabel = labelFor(option);
                          const sameLabel = group.options.filter(
                            (candidate) => labelFor(candidate) === baseLabel,
                          ).length;
                          // Doubao's Coding Plan and Agent Plan are both agent
                          // plans: keep the plan name when the capability label
                          // would otherwise print twice.
                          const chipLabel =
                            option.planName &&
                            (sameLabel > 1 || option.planName !== baseLabel)
                              ? `${baseLabel} · ${option.planName}`
                              : baseLabel;
                          return (
                            <button
                              key={option.key}
                              type="button"
                              aria-pressed={active}
                              aria-label={`${chipLabel}${
                                showRegion && option.region !== 'local'
                                  ? ` ${option.region === 'cn' ? t('setup.accessRegionCn') : t('setup.accessRegionIntl')}`
                                  : ''
                              }`}
                              onClick={(event) => {
                                event.stopPropagation();
                                chooseOption(group, option);
                              }}
                              className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[0.6875rem] font-medium transition-colors"
                              style={{
                                background: active ? 'var(--color-btn-primary-bg)' : 'var(--color-bg-page)',
                                color: active ? 'var(--color-btn-primary-text)' : 'var(--color-text-secondary)',
                                border: `1px solid ${active ? 'transparent' : 'var(--color-border-default)'}`,
                              }}
                            >
                              {chipLabel}
                              {showRegion && option.region !== 'local' && (
                                <>
                                  {' '}
                                  <span className="opacity-75">
                                    {option.region === 'cn'
                                      ? t('setup.accessRegionCn')
                                      : t('setup.accessRegionIntl')}
                                  </span>
                                </>
                              )}
                              {(configuredSet.has(option.configuredProvider) ||
                                configuredSet.has(option.provider)) && (
                                <Check className="h-2.5 w-2.5" strokeWidth={3} />
                              )}
                            </button>
                          );
                        })}
                        {/* Capabilities the provider documents but this build
                            has no selectable variant for (Qwen Token Plan today). */}
                        {familyCaps?.token_plan && !hasTokenOption && (
                          <span
                            title={t('setup.planUnavailable')}
                            className="inline-flex items-center gap-1 rounded-md border border-dashed px-1.5 py-0.5 text-[0.6875rem] font-medium"
                            style={{
                              borderColor: 'var(--color-border-default)',
                              color: 'var(--color-text-tertiary)',
                            }}
                          >
                            {t('setup.accessTokenPlan')} · {t('setup.planNotConfigured')}
                          </span>
                        )}
                        {familyCaps?.agent_plan && !hasAgentOption && !hasOauthOption && (
                          <span
                            title={t('setup.planUnavailable')}
                            className="inline-flex items-center gap-1 rounded-md border border-dashed px-1.5 py-0.5 text-[0.6875rem] font-medium"
                            style={{
                              borderColor: 'var(--color-border-default)',
                              color: 'var(--color-text-tertiary)',
                            }}
                          >
                            {t('setup.accessAgentPlan')} · {t('setup.planNotConfigured')}
                          </span>
                        )}
                        {missingApiRegions.map((region) => (
                          <span
                            key={`api-${region}`}
                            title={t('setup.apiRegionUnavailable')}
                            className="inline-flex items-center gap-1 rounded-md border border-dashed px-1.5 py-0.5 text-[0.6875rem] font-medium"
                            style={{
                              borderColor: 'var(--color-border-default)',
                              color: 'var(--color-text-tertiary)',
                            }}
                          >
                            {t('setup.accessApi')} {region === 'cn' ? t('setup.accessRegionCn') : t('setup.accessRegionIntl')} · {t('setup.planNotConfigured')}
                          </span>
                        ))}
                        {/* Documented regions with no configured variant. A
                            provider can ship only one endpoint variant while
                            its docs name both, so the card says so rather than
                            silently folding the other region into the same chip. */}
                        {missingRegions.map((region) => (
                          <span
                            key={region}
                            title={t('setup.apiRegionUnavailable')}
                            className="inline-flex items-center gap-1 rounded-md border border-dashed px-1.5 py-0.5 text-[0.6875rem] font-medium"
                            style={{
                              borderColor: 'var(--color-border-default)',
                              color: 'var(--color-text-tertiary)',
                            }}
                          >
                            {region === 'cn' ? t('setup.accessRegionCn') : t('setup.accessRegionIntl')} · {t('setup.planNotConfigured')}
                          </span>
                        ))}
                        {documentedApiRegions.length === 1 && documentedApiRegions[0] === 'global' && (
                          <span
                            className="inline-flex items-center rounded-md px-1.5 py-0.5 text-[0.6875rem] font-medium"
                            style={{
                              background: 'var(--color-bg-page)',
                              color: 'var(--color-text-tertiary)',
                              border: '1px solid var(--color-border-default)',
                            }}
                          >
                            {t('setup.apiRegionGlobalLabel')}
                          </span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </section>
          );
        })}
      </div>

      {/* Custom providers stay wherever the user manages them: the local tab
          owns self-hosted/private endpoints, and api-key/coding-plan flows can
          add one too. */}
      {method !== 'oauth' && (
        <div className="mt-6 flex flex-col gap-3">
          {userCustomProviders.length > 0 && (
            <div className="flex flex-col gap-2">
              {userCustomProviders.map((cp) => {
                const isSelected = selected === cp.name;
                return (
                  <button
                    key={cp.name}
                    type="button"
                    role="radio"
                    aria-checked={isSelected}
                    onClick={() => {
                      setSelected(cp.name);
                      setSelectedOption(null);
                    }}
                    className="flex items-center gap-3 rounded-lg p-3 text-left transition-colors"
                    style={{
                      border: `1px solid ${isSelected ? 'var(--color-text-primary)' : 'var(--color-border-default)'}`,
                      background: isSelected ? 'var(--color-bg-tag)' : undefined,
                    }}
                  >
                    <BrandMark name={cp.name} art={llmProviderArt(cp.name)} size="sm" />
                    <span className="text-sm" style={{ color: 'var(--color-text-primary)' }}>
                      {cp.name}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
          <button
            type="button"
            role="radio"
            aria-checked={selected === CUSTOM_PROVIDER_KEY}
            onClick={() => {
              setSelected(CUSTOM_PROVIDER_KEY);
              setSelectedOption(null);
            }}
            className="flex w-full items-center gap-3 rounded-lg p-3 text-left transition-colors"
            style={{
              border: `1px ${selected === CUSTOM_PROVIDER_KEY ? 'solid' : 'dashed'} ${
                selected === CUSTOM_PROVIDER_KEY
                  ? 'var(--color-text-primary)'
                  : 'var(--color-border-default)'
              }`,
              background: selected === CUSTOM_PROVIDER_KEY ? 'var(--color-bg-tag)' : undefined,
            }}
          >
            <div
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full"
              style={{
                background:
                  selected === CUSTOM_PROVIDER_KEY
                    ? 'var(--color-btn-primary-bg)'
                    : 'var(--color-bg-surface)',
                color:
                  selected === CUSTOM_PROVIDER_KEY
                    ? 'var(--color-btn-primary-text)'
                    : 'var(--color-text-tertiary)',
              }}
            >
              <Plus className="h-4 w-4" />
            </div>
            <div className="flex flex-col">
              <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
                {t('setup.customProvider')}
              </span>
              <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('setup.customProviderDesc')}
              </span>
            </div>
          </button>
        </div>
      )}

      <SetupInvitation />

      <SetupFooter
        primary={{
          label: t('setup.continue'),
          onClick: handleNext,
          disabled: !selected || (!isCustomSelected && !chosen),
        }}
      />
    </div>
  );
}
