/**
 * One state machine for every in-app subscription sign-in, shared by Settings →
 * Connected Accounts and the Setup wizard's OAuth connect step.
 *
 * There are three flow shapes behind the same user story:
 *
 * - `device`   — initiate returns a user code, the provider approves it on its
 *                own page, and polling turns into the account (ChatGPT Codex,
 *                Grok, Kimi).
 * - `paste`    — initiate returns an authorize URL, the provider shows a code,
 *                and the user pastes it back (Claude, Gemini).
 * - `redirect` — initiate returns an authorize URL plus a `state`; the provider
 *                redirects the browser back to `/oauth/zhipu` (BigModel) or
 *                completes on its side (Z.ai), and polling by `state` sees it
 *                land (Z.ai, BigModel).
 *
 * The surfaces own only their copy and chrome: this hook owns polling, the
 * timeout, cancellation, the post-connect cache refresh and disconnect, so the
 * two surfaces cannot drift apart on the parts that are easy to get wrong.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { queryKeys } from '@/lib/queryKeys';
import {
  initiateClaudeOAuth,
  submitClaudeCallback,
  disconnectClaudeOAuth,
  initiateCodexDevice,
  pollCodexDevice,
  disconnectCodexOAuth,
  initiateGeminiOAuth,
  submitGeminiCallback,
  disconnectGeminiOAuth,
  initiateGrokDevice,
  pollGrokDevice,
  disconnectGrokOAuth,
  initiateKimiDevice,
  pollKimiDevice,
  disconnectKimiOAuth,
  initiateMimoOAuth,
  submitMimoCallback,
  disconnectMimoOAuth,
  initiateZhipuOAuth,
  pollZhipuOAuth,
  disconnectZhipuOAuth,
  initiateDeepSeekOAuth,
  getDeepSeekOAuthStatus,
  disconnectDeepSeekOAuth,
} from '@/lib/dashboardApi/api';
import type { ZhipuProvider } from '@/lib/dashboardApi/api';

export type SubscriptionFlowKind = 'device' | 'paste' | 'redirect' | 'deepseek';

/** The provider id the manifest spells, e.g. `kimi-oauth`. */
export type SubscriptionProviderId =
  | 'codex-oauth'
  | 'claude-oauth'
  | 'grok-oauth'
  | 'gemini-oauth'
  | 'kimi-oauth'
  | 'mimo-oauth'
  | 'z-ai-oauth'
  | 'z-ai-cn-oauth'
  | 'deepseek-account';

/** The short code a device flow shows, plus where to approve it. */
export interface SubscriptionDeviceCode {
  user_code: string;
  verification_url: string;
}

/** What a completed login tells the surface: who signed in, and on what plan. */
export interface SubscriptionLoginResult {
  email: string | null;
  account_id: string | null;
  plan_type: string | null;
  provider?: ZhipuProvider;
}

interface FlowBase {
  disconnect: () => Promise<unknown>;
}
interface DeviceFlow extends FlowBase {
  kind: 'device';
  initiate: () => Promise<unknown>;
  poll: () => Promise<unknown>;
}
interface PasteFlow extends FlowBase {
  kind: 'paste';
  initiate: () => Promise<unknown>;
  submit: (input: string) => Promise<unknown>;
}
interface RedirectFlow extends FlowBase {
  kind: 'redirect';
  zhipuProvider: ZhipuProvider;
}
interface DeepSeekFlow extends FlowBase {
  kind: 'deepseek';
  initiate: () => Promise<{ authorize_url?: unknown }>;
}
type Flow = DeviceFlow | PasteFlow | RedirectFlow | DeepSeekFlow;

const FLOWS: Record<string, Flow> = {
  'codex-oauth': {
    kind: 'device',
    initiate: initiateCodexDevice,
    poll: pollCodexDevice,
    disconnect: disconnectCodexOAuth,
  },
  'grok-oauth': {
    kind: 'device',
    initiate: initiateGrokDevice,
    poll: pollGrokDevice,
    disconnect: disconnectGrokOAuth,
  },
  'kimi-oauth': {
    kind: 'device',
    initiate: initiateKimiDevice,
    poll: pollKimiDevice,
    disconnect: disconnectKimiOAuth,
  },
  'claude-oauth': {
    kind: 'paste',
    initiate: initiateClaudeOAuth,
    submit: submitClaudeCallback,
    disconnect: disconnectClaudeOAuth,
  },
  'gemini-oauth': {
    kind: 'paste',
    initiate: initiateGeminiOAuth,
    submit: submitGeminiCallback,
    disconnect: disconnectGeminiOAuth,
  },
  'mimo-oauth': {
    kind: 'paste',
    // Bound per attempt rather than captured when this table is built: MiMo is
    // a newer entry than the surfaces that import this module, so its client
    // functions resolve only when a MiMo card actually runs one.
    initiate: () => initiateMimoOAuth(),
    submit: (input) => submitMimoCallback(input),
    disconnect: () => disconnectMimoOAuth(),
  },
  'z-ai-oauth': {
    kind: 'redirect',
    zhipuProvider: 'zai',
    disconnect: disconnectZhipuOAuth,
  },
  'z-ai-cn-oauth': {
    kind: 'redirect',
    zhipuProvider: 'bigmodel',
    disconnect: disconnectZhipuOAuth,
  },
  'deepseek-account': {
    kind: 'deepseek',
    // The browser lands on /oauth/callback, which hands the code to the server;
    // this tab only watches the connection status land.
    initiate: () => initiateDeepSeekOAuth(deepseekRedirectUri()),
    // Resolved per attempt so older bundles' partial API mocks keep loading.
    disconnect: () => disconnectDeepSeekOAuth(),
  },
};

/** Which kind of sign-in a provider id runs, or null for one this build does
 *  not implement (a newer manifest row reaching an older bundle). */
export function subscriptionFlowForProvider(provider: string): SubscriptionFlowKind | null {
  return FLOWS[provider]?.kind ?? null;
}

/**
 * The redirect address BigModel's console sends the browser back to.
 *
 * BigModel only registers a loopback callback, so on a loopback origin the
 * `localhost` spelling wins even when the tab was opened as `127.0.0.1` (the
 * registered address is what must match). Any other origin is still sent as
 * itself: the server rejects it with a 400 the surface explains, which beats
 * making the request look local when it is not.
 */
export function zhipuRedirectUri(): string {
  if (typeof window === 'undefined') return '/oauth/zhipu';
  const { protocol, hostname, port } = window.location;
  const loopback =
    hostname === 'localhost' ||
    hostname === '127.0.0.1' ||
    hostname === '::1' ||
    hostname === '[::1]';
  const host = loopback ? 'localhost' : hostname;
  return `${protocol}//${host}${port ? `:${port}` : ''}/oauth/zhipu`;
}

/** The DeepSeek account route's registered landing page. The Harness Host
 *  accepts a loopback callback and preserves the localhost hostname; use
 *  localhost even when the tab was opened as 127.0.0.1, the same normalization
 *  BigModel's callback requires. */
export function deepseekRedirectUri(): string {
  if (typeof window === 'undefined') return '/oauth/callback';
  const { protocol, hostname, port } = window.location;
  const loopback =
    hostname === 'localhost' ||
    hostname === '127.0.0.1' ||
    hostname === '::1' ||
    hostname === '[::1]';
  const host = loopback ? 'localhost' : hostname;
  return `${protocol}//${host}${port ? `:${port}` : ''}/oauth/callback`;
}

/** Device flows poll with the interval the provider suggested, this often else. */
const DEFAULT_DEVICE_POLL_INTERVAL_MS = 5000;
/** Zhipu asks for about one poll every three seconds. */
const DEFAULT_REDIRECT_POLL_INTERVAL_MS = 3000;
/** A device sign-in left waiting this long is called a timeout. */
const DEVICE_POLL_TIMEOUT_MS = 15 * 60 * 1000;
/** The browser round-trip plus provider-side approval, bounded. */
const REDIRECT_POLL_TIMEOUT_MS = 10 * 60 * 1000;

export interface UseSubscriptionLoginOptions {
  /** Called after the connection lands and the caches are refreshed. */
  onConnected?: (result: SubscriptionLoginResult) => void;
  /** How long a wait may run before it is called a timeout (tests shorten it). */
  timeoutMs?: number;
  /** Polling cadence when the server does not name one (tests shorten it). */
  pollIntervalMs?: number;
}

export interface SubscriptionLoginState {
  kind: SubscriptionFlowKind | null;
  starting: boolean;
  awaiting: boolean;
  polling: boolean;
  submitting: boolean;
  disconnecting: boolean;
  deviceCode: SubscriptionDeviceCode | null;
  authorizeUrl: string | null;
  /** Localized failure text for the current attempt; null when there is none. */
  error: string | null;
  start: () => Promise<void>;
  submit: (input: string) => Promise<boolean>;
  cancel: () => void;
  disconnect: () => Promise<void>;
}

interface DeviceInitiateShape {
  user_code?: unknown;
  verification_url?: unknown;
  interval?: unknown;
}
interface PasteInitiateShape {
  authorize_url?: unknown;
}
interface RedirectInitiateShape {
  authorize_url?: unknown;
  state?: unknown;
  interval?: unknown;
}
interface PollShape {
  success?: unknown;
  email?: unknown;
  account_id?: unknown;
  plan_type?: unknown;
  provider?: unknown;
}

function openInNewTab(url: string): void {
  try {
    window.open(url, '_blank', 'noopener');
  } catch {
    // A blocked popup is recoverable: every surface keeps a link to the page.
  }
}

function errorStatus(error: unknown): number | null {
  const status = (error as { response?: { status?: unknown } })?.response?.status;
  return typeof status === 'number' ? status : null;
}

/** The structured `{code, message}` the OAuth routes return for actionable
 *  account failures (e.g. a Google account with no Antigravity entitlement). */
function errorDetail(error: unknown): { code?: string; message?: string } | null {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data
    ?.detail;
  if (!detail || typeof detail !== 'object') return null;
  const shaped = detail as { code?: unknown; message?: unknown };
  return {
    code: typeof shaped.code === 'string' ? shaped.code : undefined,
    message: typeof shaped.message === 'string' ? shaped.message : undefined,
  };
}

/** The device/redirect poll result read into what a surface displays. */
function polledLoginResult(result: unknown): SubscriptionLoginResult | null {
  const shape = (result ?? {}) as PollShape;
  if (shape.success !== true) return null;
  return {
    email: typeof shape.email === 'string' ? shape.email : null,
    account_id: typeof shape.account_id === 'string' ? shape.account_id : null,
    plan_type: typeof shape.plan_type === 'string' ? shape.plan_type : null,
    provider:
      shape.provider === 'zai' || shape.provider === 'bigmodel' ? shape.provider : undefined,
  };
}

export function useSubscriptionLogin(
  provider: string,
  options: UseSubscriptionLoginOptions = {},
): SubscriptionLoginState {
  const { onConnected, timeoutMs, pollIntervalMs } = options;
  const queryClient = useQueryClient();
  const { t } = useTranslation();
  const flow = FLOWS[provider] ?? null;

  const [starting, setStarting] = useState(false);
  const [awaiting, setAwaiting] = useState(false);
  const [polling, setPolling] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [deviceCode, setDeviceCode] = useState<SubscriptionDeviceCode | null>(null);
  const [authorizeUrl, setAuthorizeUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollInFlightRef = useRef(false);
  const cancelledRef = useRef(false);

  const stopPolling = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    pollInFlightRef.current = false;
    setPolling(false);
  }, []);

  // Leaving the page abandons the wait: the provider keeps its own state, so a
  // remount starts a fresh attempt rather than reviving this timer.
  useEffect(() => {
    cancelledRef.current = false;
    return () => {
      cancelledRef.current = true;
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, []);

  const refreshAfterConnect = useCallback(async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.oauth.all }),
      queryClient.invalidateQueries({ queryKey: queryKeys.user.me() }),
      // A login that mints a coding-plan key (MiMo) writes a BYOK key, so the
      // key lists have to refresh too, not only the OAuth status queries.
      queryClient.invalidateQueries({ queryKey: queryKeys.user.apiKeys() }),
      queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() }),
    ]);
  }, [queryClient]);

  const finishSuccess = useCallback(
    async (result: SubscriptionLoginResult) => {
      setAwaiting(false);
      setDeviceCode(null);
      setAuthorizeUrl(null);
      setError(null);
      await refreshAfterConnect();
      if (!cancelledRef.current) onConnected?.(result);
    },
    [refreshAfterConnect, onConnected],
  );

  const startPolling = useCallback(
    (
      poll: () => Promise<unknown>,
      intervalMs: number,
      timeoutAfterMs: number,
    ) => {
      const startedAt = Date.now();
      setPolling(true);
      timerRef.current = setInterval(() => {
        if (cancelledRef.current || pollInFlightRef.current) return;
        if (Date.now() - startedAt > timeoutAfterMs) {
          stopPolling();
          setError(t('subscription.timeout'));
          return;
        }
        pollInFlightRef.current = true;
        void (async () => {
          try {
            const result = await poll();
            if (cancelledRef.current) return;
            const loginResult = polledLoginResult(result);
            if (loginResult) {
              stopPolling();
              await finishSuccess(loginResult);
            }
          } catch {
            if (cancelledRef.current) return;
            stopPolling();
            setError(t('subscription.pollFailed'));
          } finally {
            pollInFlightRef.current = false;
          }
        })();
      }, intervalMs);
    },
    [finishSuccess, stopPolling, t],
  );

  const start = useCallback(async () => {
    if (!flow) {
      setError(t('subscription.unsupported'));
      return;
    }
    setStarting(true);
    setError(null);
    setDeviceCode(null);
    setAuthorizeUrl(null);
    try {
      if (flow.kind === 'device') {
        const data = (await flow.initiate()) as DeviceInitiateShape;
        const userCode = typeof data.user_code === 'string' ? data.user_code : '';
        const verificationUrl =
          typeof data.verification_url === 'string' ? data.verification_url : '';
        if (!userCode || !verificationUrl) throw new Error('malformed device response');
        setDeviceCode({ user_code: userCode, verification_url: verificationUrl });
        setAwaiting(true);
        openInNewTab(verificationUrl);
        const intervalMs =
          typeof data.interval === 'number' && data.interval > 0
            ? data.interval * 1000
            : (pollIntervalMs ?? DEFAULT_DEVICE_POLL_INTERVAL_MS);
        startPolling(flow.poll, intervalMs, timeoutMs ?? DEVICE_POLL_TIMEOUT_MS);
        return;
      }

      if (flow.kind === 'paste') {
        const data = (await flow.initiate()) as PasteInitiateShape;
        const url = typeof data.authorize_url === 'string' ? data.authorize_url : '';
        if (!url) throw new Error('malformed authorize response');
        setAuthorizeUrl(url);
        setAwaiting(true);
        openInNewTab(url);
        return;
      }

      if (flow.kind === 'deepseek') {
        const data = await flow.initiate();
        const url = typeof data.authorize_url === 'string' ? data.authorize_url : '';
        if (!url) throw new Error('malformed authorize response');
        setAuthorizeUrl(url);
        setAwaiting(true);
        openInNewTab(url);
        startPolling(
          async () => {
            const status = await getDeepSeekOAuthStatus();
            return status?.connected === true ? { success: true } : { success: false };
          },
          pollIntervalMs ?? DEFAULT_REDIRECT_POLL_INTERVAL_MS,
          timeoutMs ?? REDIRECT_POLL_TIMEOUT_MS,
        );
        return;
      }

      const redirectUri =
        flow.zhipuProvider === 'bigmodel' ? zhipuRedirectUri() : undefined;
      const data = (await initiateZhipuOAuth(
        flow.zhipuProvider,
        redirectUri,
      )) as RedirectInitiateShape;
      const url = typeof data.authorize_url === 'string' ? data.authorize_url : '';
      const state = typeof data.state === 'string' ? data.state : '';
      if (!url || !state) throw new Error('malformed redirect response');
      setAuthorizeUrl(url);
      setAwaiting(true);
      openInNewTab(url);
      const intervalMs =
        typeof data.interval === 'number' && data.interval > 0
          ? data.interval * 1000
          : (pollIntervalMs ?? DEFAULT_REDIRECT_POLL_INTERVAL_MS);
      startPolling(
        () => pollZhipuOAuth(state),
        intervalMs,
        timeoutMs ?? REDIRECT_POLL_TIMEOUT_MS,
      );
    } catch (caught: unknown) {
      setAwaiting(false);
      setDeviceCode(null);
      setAuthorizeUrl(null);
      // The server only rejects BigModel's redirect_uri (400) for a reason the
      // user can act on: the app is not on loopback.
      const needsLoopback =
        flow.kind === 'redirect' &&
        flow.zhipuProvider === 'bigmodel' &&
        errorStatus(caught) === 400;
      setError(t(needsLoopback ? 'subscription.zhipuLoopbackRequired' : 'subscription.startFailed'));
    } finally {
      setStarting(false);
    }
  }, [flow, pollIntervalMs, startPolling, t, timeoutMs]);

  const submit = useCallback(
    async (input: string): Promise<boolean> => {
      if (!flow || flow.kind !== 'paste') return false;
      const value = input.trim();
      if (!value) return false;
      setSubmitting(true);
      setError(null);
      try {
        const result = (await flow.submit(value)) as PollShape;
        if (result.success === true) {
          await finishSuccess(polledLoginResult(result) ?? {
            email: null,
            account_id: null,
            plan_type: null,
          });
          return true;
        }
        setError(t('subscription.invalidCode'));
        return false;
      } catch (caught: unknown) {
        const detail = errorDetail(caught);
        if (detail?.code === 'gemini_ineligible' || detail?.code === 'gemini_consumer_oauth_retired') {
          setError(t('subscription.geminiAntigravityIneligible'));
        } else {
          setError(t('subscription.invalidCode'));
        }
        return false;
      } finally {
        setSubmitting(false);
      }
    },
    [finishSuccess, flow, t],
  );

  const cancel = useCallback(() => {
    stopPolling();
    setAwaiting(false);
    setDeviceCode(null);
    setAuthorizeUrl(null);
    setError(null);
  }, [stopPolling]);

  const disconnect = useCallback(async () => {
    if (!flow) return;
    setDisconnecting(true);
    setError(null);
    try {
      await flow.disconnect();
      await refreshAfterConnect();
    } catch {
      setError(t('subscription.disconnectFailed'));
    } finally {
      setDisconnecting(false);
    }
  }, [flow, refreshAfterConnect, t]);

  return {
    kind: flow?.kind ?? null,
    starting,
    awaiting,
    polling,
    submitting,
    disconnecting,
    deviceCode,
    authorizeUrl,
    error,
    start,
    submit,
    cancel,
    disconnect,
  };
}
