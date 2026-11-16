import { useCallback, useId, useState } from 'react';
import { useNavigate } from 'react-router';
import { ExternalLink, AlertTriangle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Loader } from '@/components/ui/loader';
import { Input } from '@/components/ui/input';
import { WizardStepHeader } from '@/components/ui/wizard';
import { BrandMark } from '@/components/mcp/BrandMark';
import { llmProviderArt } from '@/lib/brandArt';
import { useTranslation } from 'react-i18next';
import {
  subscriptionFlowForProvider,
  useSubscriptionLogin,
  type SubscriptionFlowKind,
} from '@/components/model/useSubscriptionLogin';
import {
  DeviceCodePrompt,
  RedirectLoginPrompt,
} from '@/components/model/SubscriptionLoginPanels';
import { ProcessStep, DisclaimerBox, useProviderConnected, type LocationState } from './shared';
import { DeepSeekConnect } from './DeepSeekConnect';
import { SetupFooter } from '../../SetupFooter';

/** The three steps each flow shape walks the user through; kept as keys so the
 *  same explanation serves every provider with that shape. */
const FLOW_PROCESS_STEPS: Record<
  SubscriptionFlowKind,
  ReadonlyArray<{ titleKey: string; descriptionKey: string }>
> = {
  device: [
    { titleKey: 'setup.oauthDeviceStep1Title', descriptionKey: 'setup.oauthDeviceStep1Desc' },
    { titleKey: 'setup.oauthDeviceStep2Title', descriptionKey: 'setup.oauthDeviceStep2Desc' },
    { titleKey: 'setup.oauthDeviceStep3Title', descriptionKey: 'setup.oauthDeviceStep3Desc' },
  ],
  paste: [
    { titleKey: 'setup.oauthPasteStep1Title', descriptionKey: 'setup.oauthPasteStep1Desc' },
    { titleKey: 'setup.oauthPasteStep2Title', descriptionKey: 'setup.oauthPasteStep2Desc' },
    { titleKey: 'setup.oauthPasteStep3Title', descriptionKey: 'setup.oauthPasteStep3Desc' },
  ],
  redirect: [
    { titleKey: 'setup.oauthRedirectStep1Title', descriptionKey: 'setup.oauthRedirectStep1Desc' },
    { titleKey: 'setup.oauthRedirectStep2Title', descriptionKey: 'setup.oauthRedirectStep2Desc' },
    { titleKey: 'setup.oauthRedirectStep3Title', descriptionKey: 'setup.oauthRedirectStep3Desc' },
  ],
  // DeepSeek's dedicated step renders its own copy; this keeps the shared map
  // total for the flow kind the hook reports.
  deepseek: [
    { titleKey: 'setup.oauthRedirectStep1Title', descriptionKey: 'setup.oauthRedirectStep1Desc' },
    { titleKey: 'setup.oauthRedirectStep2Title', descriptionKey: 'setup.oauthRedirectStep2Desc' },
    { titleKey: 'setup.oauthRedirectStep3Title', descriptionKey: 'setup.oauthRedirectStep3Desc' },
  ],
};

/** The sign-in step for every OAuth/subscription provider in the manifest:
 *  device code (Codex, Grok, Kimi), paste-back (Claude, Gemini), or the browser
 *  round-trip Zhipu/Z.ai runs. The flow mechanics live in
 *  `useSubscriptionLogin`, which Settings → Connected Accounts shares. */
export function OAuthConnect({ state }: { state: LocationState }) {
  const navigate = useNavigate();
  const connected = useProviderConnected();
  const { t } = useTranslation();
  const headingId = useId();
  const pasteLabelId = useId();
  const pasteHintId = useId();

  const method = state.method ?? 'api_key';
  const provider = state.provider ?? '';
  const displayName = state.displayName ?? provider;
  const brandKey = state.brandKey ?? provider;
  const flowKind = subscriptionFlowForProvider(provider);

  const [agreed, setAgreed] = useState(false);
  const [pasteInput, setPasteInput] = useState('');

  const handleConnected = useCallback(() => {
    connected({ method, provider, displayName, brandKey });
  }, [connected, method, provider, displayName, brandKey]);

  const login = useSubscriptionLogin(provider, { onConnected: handleConnected });

  const handleBack = useCallback(() => {
    navigate('/setup/provider', { state: { method } });
  }, [navigate, method]);

  const handleStart = useCallback(() => {
    void login.start();
  }, [login]);

  const handleCancel = useCallback(() => {
    login.cancel();
    setPasteInput('');
  }, [login]);

  const handlePasteSubmit = useCallback(async () => {
    const completed = await login.submit(pasteInput);
    if (completed) setPasteInput('');
  }, [login, pasteInput]);

  const unsupported = (
    <div>
      <WizardStepHeader
        titleId={headingId}
        title={t('setup.connectTitle', { provider: displayName })}
      />
      <p role="alert" className="text-sm" style={{ color: 'var(--color-loss)' }}>
        {t('setup.oauthUnsupported')}
      </p>
      <SetupFooter back={{ label: t('setup.back'), onClick: handleBack }} primary={null} />
    </div>
  );

  // DeepSeek's account route is its own browser round-trip (no generic poll).
  if (provider === 'deepseek-account') return <DeepSeekConnect state={state} />;

  if (!flowKind) return unsupported;

  const helperKey =
    flowKind === 'device'
      ? 'setup.oauthDeviceDesc'
      : flowKind === 'paste'
        ? 'setup.oauthPasteDesc'
        : 'setup.oauthRedirectDesc';
  const startLabel =
    flowKind === 'device'
      ? t('setup.openAuthorization', { provider: displayName })
      : t('setup.openLogin', { provider: displayName });
  // A waiting panel shows its own failure; every other failure belongs below.
  const panelShowsError = login.awaiting && flowKind !== 'paste';

  return (
    <div>
      <WizardStepHeader
        titleId={headingId}
        icon={
          <BrandMark
            name={displayName}
            art={llmProviderArt(provider || brandKey)}
            size="lg"
          />
        }
        title={t('setup.connectTitle', { provider: displayName })}
        helper={t(helperKey, { provider: displayName })}
      />
      <div className="flex flex-col gap-4 sm:gap-6">
        {/* Phase 1: disclaimer + process flow, before anything opens */}
        {!login.starting && !login.awaiting && (
          <>
            <div
              className="rounded-lg p-4"
              style={{
                background: 'var(--color-bg-surface)',
                border: '1px solid var(--color-border-default)',
              }}
            >
              <p
                className="text-xs font-medium uppercase tracking-wide mb-3"
                style={{ color: 'var(--color-text-tertiary)' }}
              >
                {t('setup.howItWorks')}
              </p>
              <div className="space-y-3">
                {FLOW_PROCESS_STEPS[flowKind].map((step, index) => (
                  <ProcessStep
                    key={step.titleKey}
                    number={index + 1}
                    title={t(step.titleKey, { provider: displayName })}
                    description={t(step.descriptionKey, { provider: displayName })}
                  />
                ))}
              </div>
            </div>

            <DisclaimerBox providerName={displayName} />

            <label className="flex items-start gap-3 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={agreed}
                onChange={(e) => setAgreed(e.target.checked)}
                className="mt-0.5 h-4 w-4 rounded border accent-(--color-btn-primary-bg)"
              />
              <span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                {t('setup.oauthAgree')}
              </span>
            </label>

            {/* Opens the provider's sign-in: the step's own action, so it
                stays here and the footer carries only Back. Finishing the
                sign-in is what moves the step on. */}
            <Button
              variant="default"
              disabled={!agreed || login.starting}
              onClick={handleStart}
              className="w-full h-11"
            >
              <ExternalLink className="h-4 w-4 mr-1.5" />
              {startLabel}
            </Button>
          </>
        )}

        {/* Phase 2: starting the flow */}
        {login.starting && (
          <div role="status" className="flex items-center justify-center py-12">
            <span aria-hidden="true" className="shrink-0">
              <Loader size={24} className="text-(--color-accent-primary)" />
            </span>
            <span className="ml-2 text-sm" style={{ color: 'var(--color-text-secondary)' }}>
              {t('setup.connecting')}
            </span>
          </div>
        )}

        {/* Phase 3: active — device code to approve on the provider's page */}
        {login.awaiting && login.kind === 'device' && login.deviceCode && (
          <div
            className="flex flex-col gap-3 rounded-lg p-5"
            style={{
              background: 'var(--color-bg-surface)',
              border: '1px solid var(--color-border-default)',
            }}
          >
            <div className="flex items-center gap-2">
              <AlertTriangle className="h-4 w-4 shrink-0" style={{ color: 'var(--color-warning, #f59e0b)' }} />
              <p className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
                {t('setup.enterCodeOnProvider', { provider: displayName })}
              </p>
            </div>
            <DeviceCodePrompt
              deviceCode={login.deviceCode}
              polling={login.polling}
              error={login.error}
              onCancel={handleCancel}
            />
          </div>
        )}

        {/* Phase 3: active — paste the code the provider showed */}
        {login.awaiting && login.kind === 'paste' && login.authorizeUrl && (
          <div
            className="flex flex-col gap-4 rounded-lg p-5"
            style={{
              background: 'var(--color-bg-surface)',
              border: '1px solid var(--color-border-default)',
            }}
          >
            <div className="flex flex-col gap-1">
              <p id={pasteLabelId} className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
                {t('setup.pasteAuthCode')}
              </p>
              <p id={pasteHintId} className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('setup.pasteAuthCodeHint')}
              </p>
            </div>

            <div className="flex flex-wrap gap-2">
              <Input
                aria-labelledby={pasteLabelId}
                aria-describedby={pasteHintId}
                aria-invalid={!!login.error}
                value={pasteInput}
                onChange={(e) => setPasteInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void handlePasteSubmit();
                }}
                placeholder={t('setup.pasteCodePlaceholder')}
                className="flex-1 min-w-0 font-mono text-sm"
                autoComplete="off"
                spellCheck={false}
              />
              <Button
                variant="default"
                disabled={login.submitting || !pasteInput.trim()}
                onClick={() => { void handlePasteSubmit(); }}
                className="shrink-0"
              >
                {login.submitting ? (
                  <>
                    <span aria-hidden="true" className="mr-1.5 shrink-0">
                      <Loader size={16} className="text-current" />
                    </span>
                    {t('setup.submitting')}
                  </>
                ) : (
                  t('setup.submit')
                )}
              </Button>
            </div>

            <div className="flex flex-wrap items-center gap-3">
              <a
                href={login.authorizeUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 text-xs underline underline-offset-2 hover:opacity-80"
                style={{ color: 'var(--color-text-primary)' }}
              >
                {t('setup.openLoginAgain', { provider: displayName })}
                <ExternalLink className="h-3 w-3" />
              </a>
              <button
                type="button"
                onClick={handleCancel}
                className="px-3 py-1.5 rounded-md text-xs font-medium"
                style={{ color: 'var(--color-text-tertiary)', backgroundColor: 'transparent' }}
              >
                {t('common.cancel')}
              </button>
            </div>
          </div>
        )}

        {/* Phase 3: active — waiting for the browser round-trip */}
        {login.awaiting && login.kind === 'redirect' && (
          <div
            className="rounded-lg p-5"
            style={{
              background: 'var(--color-bg-surface)',
              border: '1px solid var(--color-border-default)',
            }}
          >
            <RedirectLoginPrompt
              authorizeUrl={login.authorizeUrl}
              polling={login.polling}
              error={login.error}
              onCancel={handleCancel}
            />
          </div>
        )}

        {/* Failure with no waiting panel of its own to report it. */}
        {login.error && !panelShowsError && (
          <p role="alert" className="text-sm" style={{ color: 'var(--color-loss)' }}>
            {login.error}
          </p>
        )}
      </div>

      <SetupFooter back={{ label: t('setup.back'), onClick: handleBack }} primary={null} />
    </div>
  );
}
