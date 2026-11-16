/**
 * DeepSeek account sign-in step.
 *
 * The browser round-trip is DeepSeek's own: `initiate` returns the platform's
 * authorize URL, the browser sends the user back to `/oauth/callback` with a
 * code + state pair, and that page hands the pair to the server. This tab
 * polls the connection status and moves the wizard on when the token lands.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useQueryClient } from '@tanstack/react-query';
import { WizardStepHeader } from '@/components/ui/wizard';
import { BrandMark } from '@/components/mcp/BrandMark';
import { llmProviderArt } from '@/lib/brandArt';
import { RedirectLoginPrompt } from '@/components/model/SubscriptionLoginPanels';
import { deepseekRedirectUri } from '@/components/model/useSubscriptionLogin';
import { queryKeys } from '@/lib/queryKeys';
import {
  getDeepSeekOAuthStatus,
  initiateDeepSeekOAuth,
} from '@/lib/dashboardApi/api';
import { SetupFooter } from '../../SetupFooter';
import { useProviderConnected, type LocationState } from './shared';

const POLL_INTERVAL_MS = 3000;
const TIMEOUT_MS = 10 * 60 * 1000;

export function DeepSeekConnect({ state }: { state: LocationState }) {
  const navigate = useNavigate();
  const connected = useProviderConnected();
  const queryClient = useQueryClient();
  const { t } = useTranslation();

  const method = state.method ?? 'oauth';
  const provider = state.provider ?? 'deepseek-account';
  const displayName = state.displayName ?? 'DeepSeek';
  const brandKey = state.brandKey ?? 'deepseek';

  const [starting, setStarting] = useState(false);
  const [awaiting, setAwaiting] = useState(false);
  const [authorizeUrl, setAuthorizeUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const stopPolling = useCallback(() => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  useEffect(() => () => stopPolling(), [stopPolling]);

  const onConnected = useCallback(async () => {
    stopPolling();
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.oauth.providers() }),
      queryClient.invalidateQueries({ queryKey: queryKeys.user.me() }),
      queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() }),
    ]).catch(() => undefined);
    connected({ method, provider, displayName, brandKey });
  }, [brandKey, connected, displayName, method, provider, queryClient, stopPolling]);

  const startPolling = useCallback(() => {
    const startedAt = Date.now();
    const tick = async () => {
      try {
        const status = await getDeepSeekOAuthStatus();
        if (status?.connected) {
          await onConnected();
          return;
        }
      } catch {
        // A transient read failure is not a failed sign-in; keep waiting.
      }
      if (Date.now() - startedAt > TIMEOUT_MS) {
        setAwaiting(false);
        setError(t('setup.deepseekTimeout', '登录超时，请重试。'));
        return;
      }
      timer.current = setTimeout(tick, POLL_INTERVAL_MS);
    };
    timer.current = setTimeout(tick, POLL_INTERVAL_MS);
  }, [onConnected, t]);

  const start = useCallback(async () => {
    setStarting(true);
    setError(null);
    try {
      const { authorize_url: url } = await initiateDeepSeekOAuth(deepseekRedirectUri());
      setAuthorizeUrl(url);
      window.open(url, '_blank', 'noopener,noreferrer');
      setAwaiting(true);
      startPolling();
    } catch {
      setError(t('setup.deepseekStartFailed', '无法启动 DeepSeek 登录，请稍后重试。'));
    } finally {
      setStarting(false);
    }
  }, [startPolling, t]);

  const cancel = useCallback(() => {
    stopPolling();
    setAwaiting(false);
    setAuthorizeUrl(null);
    setError(null);
  }, [stopPolling]);

  return (
    <div>
      <WizardStepHeader
        titleId="deepseek-connect-title"
        icon={
          <BrandMark
            name={displayName}
            art={llmProviderArt(provider || brandKey)}
            size="lg"
          />
        }
        title={t('setup.connectTitle', { provider: displayName })}
        helper={t(
          'setup.deepseekConnectDesc',
          '使用 DeepSeek 平台账号登录。浏览器完成授权后会自动回到 Conductor，本页检测到登录即继续。',
        )}
      />
      <div className="flex flex-col gap-4">
        <RedirectLoginPrompt
          authorizeUrl={authorizeUrl}
          polling={awaiting}
          error={error}
          onCancel={cancel}
        />
      </div>
      <SetupFooter
        back={{ label: t('setup.back'), onClick: () => navigate('/setup/provider', { state: { method } }) }}
        primary={
          authorizeUrl
            ? null
            : {
                label: starting ? t('setup.connecting', '正在连接…') : t('setup.openLogin', { provider: displayName }),
                onClick: () => void start(),
                busy: starting,
              }
        }
      />
    </div>
  );
}
