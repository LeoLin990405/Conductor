/**
 * Landing page for the DeepSeek account OAuth redirect (`/oauth/callback`).
 *
 * DeepSeek sends the browser back here with `?code=&state=`; the server keeps
 * the PKCE verifier and performs auth_exchange, so this page only hands the
 * pair over and reports the result. The tab that started the flow polls
 * `/oauth/deepseek/status` and closes the loop when the token lands.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Loader } from '@/components/ui/loader';
import { submitDeepSeekCallback } from '@/lib/dashboardApi/api';

type CallbackPhase = 'pending' | 'success' | 'error';

function readParams(search: string, hash = ''): { code: string | null; state: string | null } {
  // DeepSeek normally appends ?code=&state=, but a fragment is accepted too so
  // a client-side route never silently drops a valid callback.
  const fromSearch = new URLSearchParams(search);
  const fromHash = new URLSearchParams(hash.replace(/^#/, ''));
  const pick = (key: string) =>
    (fromSearch.get(key) || fromHash.get(key) || '').trim() || null;
  return { code: pick('code'), state: pick('state') };
}

export default function DeepSeekCallback() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [params] = useState(() => readParams(window.location.search, window.location.hash));
  const [phase, setPhase] = useState<CallbackPhase>(() =>
    params.code && params.state ? 'pending' : 'error',
  );

  useEffect(() => {
    if (!params.code || !params.state) return;
    let cancelled = false;
    void (async () => {
      try {
        const result = await submitDeepSeekCallback(params.code!, params.state!);
        if (!cancelled) setPhase(result?.success ? 'success' : 'error');
      } catch {
        if (!cancelled) setPhase('error');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [params]);

  const back = useCallback(() => {
    if (window.opener) window.close();
    else navigate('/setup/models', { replace: true });
  }, [navigate]);

  return (
    <div
      className="flex flex-col items-center justify-center gap-4 min-h-screen px-6 text-center"
      style={{ backgroundColor: 'var(--color-bg-page)' }}
    >
      <h1 className="text-lg font-medium" style={{ color: 'var(--color-text-primary)' }}>
        {t('deepseekCallback.title')}
      </h1>
      {phase === 'pending' && (
        <div role="status" className="flex items-center gap-2">
          <span aria-hidden="true" className="shrink-0">
            <Loader size={18} className="text-current" />
          </span>
          <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
            {t('deepseekCallback.pending')}
          </p>
        </div>
      )}
      {phase === 'success' && (
        <p role="status" className="text-sm max-w-sm" style={{ color: 'var(--color-text-secondary)' }}>
          {t('deepseekCallback.success')}
        </p>
      )}
      {phase === 'error' && (
        <p role="alert" className="text-sm max-w-sm" style={{ color: 'var(--color-loss)' }}>
          {t('deepseekCallback.failure')}
        </p>
      )}
      {phase !== 'pending' && (
        <button
          type="button"
          onClick={back}
          className="inline-flex h-9 items-center rounded-md border px-3 text-sm"
          style={{ borderColor: 'var(--color-border-default)', color: 'var(--color-text-primary)' }}
        >
          {t('deepseekCallback.back')}
        </button>
      )}
    </div>
  );
}
