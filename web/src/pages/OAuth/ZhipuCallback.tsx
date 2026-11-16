/**
 * Landing page for the BigModel OAuth redirect (`/oauth/zhipu`).
 *
 * BigModel sends the browser back here with `?code=&state=`, and the server
 * owns the token exchange, so this page does one job: hand the pair to
 * `POST /oauth/zhipu/callback` with the same redirect URI the flow started
 * with, then say what happened. On success it leaves for Settings, where the
 * model tab shows the connected account. It is a top-level route (near
 * `/callback`) so it works in OSS mode too, and it navigates to a fixed
 * destination so the credential-bearing query never rides along.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader } from '@/components/ui/loader';
import { queryKeys } from '@/lib/queryKeys';
import { submitZhipuCallback } from '@/lib/dashboardApi/api';
import { zhipuRedirectUri } from '@/components/model/useSubscriptionLogin';

/** How long the success copy is left up before Settings replaces it. */
export const SUCCESS_REDIRECT_MS = 1200;

type CallbackPhase = 'pending' | 'success' | 'error';

/** BigModel spells the parameter `authCode`; other OAuth consoles use `code`. */
function readCallbackParams(search: string): { code: string | null; state: string | null } {
  const params = new URLSearchParams(search);
  const code = params.get('authCode') ?? params.get('code');
  return {
    code: code?.trim() || null,
    state: params.get('state')?.trim() || null,
  };
}

export default function ZhipuCallback() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  // Read once, at mount: a re-render must not offer to exchange a single-use
  // code a second time. A redirect that cannot even be attempted starts in the
  // failure state, so the effect below only ever marks a completed exchange.
  const [params] = useState(() => readCallbackParams(location.search));
  const [phase, setPhase] = useState<CallbackPhase>(() =>
    params.code && params.state ? 'pending' : 'error',
  );
  const submittedRef = useRef(false);

  const goToSettings = useCallback(() => {
    navigate('/settings?tab=model', { replace: true });
  }, [navigate]);

  useEffect(() => {
    if (!params.code || !params.state || submittedRef.current) return;
    submittedRef.current = true;
    const code = params.code;
    const state = params.state;
    let cancelled = false;
    void (async () => {
      try {
        const result = await submitZhipuCallback(code, state, zhipuRedirectUri());
        if (cancelled) return;
        if (result?.success) {
          await Promise.all([
            queryClient.invalidateQueries({ queryKey: queryKeys.oauth.zhipu() }),
            queryClient.invalidateQueries({ queryKey: queryKeys.user.me() }),
            queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() }),
          ]);
          if (!cancelled) setPhase('success');
        } else {
          setPhase('error');
        }
      } catch {
        // Sanitized copy on purpose: the failure detail is the server's, not
        // something a redirect page should put on screen.
        if (!cancelled) setPhase('error');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [params, queryClient]);

  useEffect(() => {
    if (phase !== 'success') return;
    const timer = setTimeout(goToSettings, SUCCESS_REDIRECT_MS);
    return () => clearTimeout(timer);
  }, [phase, goToSettings]);

  return (
    <div
      className="flex flex-col items-center justify-center gap-4 min-h-screen px-6 text-center"
      style={{ backgroundColor: 'var(--color-bg-page)' }}
    >
      <h1 className="text-lg font-medium" style={{ color: 'var(--color-text-primary)' }}>
        {t('zhipuCallback.title')}
      </h1>

      {phase === 'pending' && (
        <div role="status" className="flex items-center gap-2">
          <span aria-hidden="true" className="shrink-0">
            <Loader size={18} className="text-current" />
          </span>
          <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
            {t('zhipuCallback.pending')}
          </p>
        </div>
      )}

      {phase === 'success' && (
        <p role="status" className="text-sm max-w-sm" style={{ color: 'var(--color-text-secondary)' }}>
          {t('zhipuCallback.success')}
        </p>
      )}

      {phase === 'error' && (
        <p role="alert" className="text-sm max-w-sm" style={{ color: 'var(--color-loss)' }}>
          {t('zhipuCallback.failure')}
        </p>
      )}

      {phase !== 'pending' && (
        <button
          type="button"
          onClick={goToSettings}
          className="px-4 py-2 rounded-md text-sm font-medium transition-opacity hover:opacity-90"
          style={{
            backgroundColor: 'var(--color-btn-primary-bg)',
            color: 'var(--color-btn-primary-text)',
          }}
        >
          {t('zhipuCallback.goToSettings')}
        </button>
      )}
    </div>
  );
}
