import { useEffect, useState } from 'react';
import { Bot } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';

import { useNativeAgents } from '@/hooks/useNativeAgents';
import { nativeRuntimeLabel } from '@/lib/nativeRuntimeLabels';
import {
  cancelNativeLogin,
  getNativeAuthStatuses,
  getNativeLogin,
  startNativeLogin,
  submitNativeLoginCode,
  type NativeLoginSession,
} from '@/api/nativeAgents';

const CAPABILITY_KEYS = [
  ['streaming', 'streaming'],
  ['tool_events', 'toolEvents'],
  ['approvals', 'approvals'],
  ['mcp', 'mcp'],
  ['memory', 'memory'],
  ['skills', 'skills'],
  ['cancel', 'cancel'],
] as const;

const LOGIN_PROVIDERS = new Set(['claude-code', 'cursor', 'codex', 'kimi', 'zcode']);

function isPending(session: NativeLoginSession | null): boolean {
  return session?.status === 'waiting' || session?.status === 'input_required';
}

/** Inventory of the host-native CLI lane plus the official sign-in panels.
 *  The backend is the source of truth for installation, live proof,
 *  capabilities and auth state; this panel never derives them locally. */
export function NativeRuntimeInventory() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data: runtimes, isLoading } = useNativeAgents(true);
  const [login, setLogin] = useState<NativeLoginSession | null>(null);
  const [code, setCode] = useState('');

  const authQuery = useQuery({
    queryKey: ['nativeAgents', 'auth'],
    queryFn: getNativeAuthStatuses,
    staleTime: 30_000,
    retry: false,
  });

  const sessionQuery = useQuery({
    queryKey: ['nativeAgents', 'login', login?.session_id],
    queryFn: () => getNativeLogin(login!.session_id),
    enabled: Boolean(login?.session_id) && isPending(login),
    refetchInterval: 1500,
    retry: false,
  });

  useEffect(() => {
    if (!sessionQuery.data) return;
    setLogin(sessionQuery.data);
    if (sessionQuery.data.status === 'authenticated') {
      void queryClient.invalidateQueries({ queryKey: ['nativeAgents'] });
    }
  }, [sessionQuery.data, queryClient]);

  const start = useMutation({
    mutationFn: startNativeLogin,
    onSuccess: (session) => {
      setLogin(session);
      setCode('');
      if (session.status === 'authenticated') {
        void queryClient.invalidateQueries({ queryKey: ['nativeAgents'] });
      }
    },
  });

  const submit = useMutation({
    mutationFn: ({ sessionId, value }: { sessionId: string; value: string }) =>
      submitNativeLoginCode(sessionId, value),
    onSuccess: (session) => {
      setLogin(session);
      setCode('');
    },
  });

  const cancel = useMutation({
    mutationFn: cancelNativeLogin,
    onSuccess: (session) => setLogin(session),
  });

  const authByProvider = new Map(
    (authQuery.data ?? []).map((row) => [row.provider, row]),
  );

  const activeLogin = login;
  const error = start.error ?? submit.error ?? cancel.error;

  return (
    <section
      className="max-w-2xl space-y-3 rounded-(--radius-card) p-4"
      style={{ backgroundColor: 'var(--color-bg-card)', border: '1px solid var(--color-border-muted)' }}
      data-testid="native-runtime-inventory"
    >
      <div className="flex items-start gap-3">
        <Bot className="mt-0.5 h-4 w-4 shrink-0" style={{ color: 'var(--color-accent-primary)' }} />
        <div className="min-w-0">
          <h3 className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {t('settings.nativeRuntimeInventory.title', 'Native agent runtimes')}
          </h3>
          <p className="mt-1 text-sm leading-relaxed" style={{ color: 'var(--color-text-secondary)' }}>
            {t(
              'settings.nativeRuntimeInventory.description',
              'The backend inventory is authoritative. Official sign-in runs the vendor CLI itself; Conductor never stores the token.',
            )}
          </p>
          <p className="mt-1 text-xs leading-relaxed" style={{ color: 'var(--color-text-tertiary)' }}>
            {t(
              'settings.nativeRuntimeInventory.deepseekHint',
              'DeepSeek account / API key is configured in API & models.',
            )}{' '}
            <Link to="/settings?tab=model" className="underline" style={{ color: 'var(--color-accent-primary)' }}>
              {t('settings.nativeRuntimeInventory.openApiSettings', 'Open API settings')}
            </Link>
          </p>
        </div>
      </div>

      {error && (
        <p className="text-xs" style={{ color: 'var(--color-danger-soft)' }}>
          {String((error as Error).message || error)}
        </p>
      )}

      {isLoading && !runtimes ? (
        <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{t('common.loading')}</p>
      ) : !runtimes || runtimes.length === 0 ? (
        <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('settings.nativeRuntimeInventory.empty', 'No native runtimes are installed on this host.')}
        </p>
      ) : (
        <ul className="space-y-2">
          {runtimes.map((runtime) => {
            const caps = CAPABILITY_KEYS.filter(([key]) => runtime.capabilities[key]).map(
              ([, label]) => t(`settings.nativeRuntimeInventory.capabilities.${label}`, label),
            );
            const auth = authByProvider.get(runtime.provider);
            const supportsLogin = LOGIN_PROVIDERS.has(runtime.provider);
            const signedIn = auth?.authenticated === true;
            const deepseek = runtime.provider === 'deepseek-dsh';
            return (
              <li
                key={runtime.provider}
                className="flex flex-wrap items-center gap-2 rounded-(--radius-control) px-3 py-2"
                style={{ backgroundColor: 'var(--color-bg-subtle)' }}
              >
                <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
                  {nativeRuntimeLabel(runtime.provider)}
                </span>
                <span
                  className="rounded-full px-2 py-0.5 text-2xs"
                  style={{
                    backgroundColor: signedIn
                      ? 'var(--color-success-soft)'
                      : runtime.live_verified
                        ? 'var(--color-success-soft)'
                        : 'var(--color-bg-card)',
                    color: signedIn || runtime.live_verified
                      ? 'var(--color-success)'
                      : 'var(--color-text-tertiary)',
                  }}
                >
                  {signedIn
                    ? t('settings.nativeRuntimeInventory.signedIn', '已登录')
                    : runtime.live_verified
                      ? t('settings.nativeRuntimeInventory.live', 'Live')
                      : runtime.installed
                        ? t('settings.nativeRuntimeInventory.installed', 'Installed, not live-verified')
                        : t('settings.nativeRuntimeInventory.notInstalled', 'Not installed')}
                </span>
                {caps.length > 0 && (
                  <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                    {caps.join(' · ')}
                  </span>
                )}
                {deepseek && (
                  <Link
                    to="/settings?tab=model"
                    className="ml-auto text-xs underline"
                    style={{ color: 'var(--color-accent-primary)' }}
                  >
                    {t('settings.nativeRuntimeInventory.deepseekLogin', 'DeepSeek 账号 / API')}
                  </Link>
                )}
                {supportsLogin && !signedIn && (
                  <button
                    type="button"
                    className="ml-auto rounded-(--radius-control) px-2.5 py-1 text-xs font-medium"
                    style={{
                      backgroundColor: 'var(--color-accent-soft)',
                      color: 'var(--color-accent-primary)',
                    }}
                    onClick={() => start.mutate(runtime.provider)}
                    disabled={start.isPending || isPending(activeLogin)}
                  >
                    {t('settings.nativeRuntimeInventory.officialLogin', '官方登录')}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {activeLogin && (
        <div
          className="space-y-2 rounded-(--radius-control) p-3"
          style={{ backgroundColor: 'var(--color-bg-card)', border: '1px solid var(--color-border-muted)' }}
        >
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
              {nativeRuntimeLabel(activeLogin.provider)}
            </span>
            <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {activeLogin.status}
            </span>
          </div>
          <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
            {activeLogin.instruction || activeLogin.error}
          </p>
          {activeLogin.auth_url && (
            <a
              href={activeLogin.auth_url}
              target="_blank"
              rel="noopener noreferrer"
              className="block break-all text-xs underline"
              style={{ color: 'var(--color-accent-primary)' }}
            >
              {t('settings.nativeRuntimeInventory.openLoginUrl', '打开官方登录页')}
            </a>
          )}
          {activeLogin.requires_input && activeLogin.session_id && (
            <form
              className="flex flex-wrap items-center gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                if (!code.trim()) return;
                submit.mutate({ sessionId: activeLogin.session_id, value: code.trim() });
              }}
            >
              <input
                value={code}
                onChange={(event) => setCode(event.target.value)}
                placeholder={t('settings.nativeRuntimeInventory.codePlaceholder', '粘贴浏览器返回的授权码')}
                className="min-w-0 flex-1 rounded-(--radius-control) border bg-transparent px-2 py-1 font-mono text-xs"
                style={{ borderColor: 'var(--color-border-muted)', color: 'var(--color-text-primary)' }}
              />
              <button
                type="submit"
                className="rounded-(--radius-control) px-2 py-1 text-xs font-medium"
                style={{ backgroundColor: 'var(--color-btn-primary-bg)', color: 'var(--color-btn-primary-text)' }}
                disabled={submit.isPending}
              >
                {t('settings.nativeRuntimeInventory.submitCode', '提交授权码')}
              </button>
            </form>
          )}
          {isPending(activeLogin) && activeLogin.session_id && (
            <button
              type="button"
              className="text-xs underline"
              style={{ color: 'var(--color-text-tertiary)' }}
              onClick={() => cancel.mutate(activeLogin.session_id)}
            >
              {t('common.cancel', 'Cancel')}
            </button>
          )}
          {!isPending(activeLogin) && (
            <button
              type="button"
              className="text-xs underline"
              style={{ color: 'var(--color-text-tertiary)' }}
              onClick={() => setLogin(null)}
            >
              {t('common.close', 'Close')}
            </button>
          )}
        </div>
      )}
    </section>
  );
}
