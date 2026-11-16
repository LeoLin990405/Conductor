/**
 * Dashboard API utilities
 * All backend endpoints used by the Dashboard page
 */
import { api } from '@/api/client';

// --- Hello ---

export async function fetchHello(): Promise<string> {
  const { data } = await api.get('/hello', { responseType: 'text' });
  return data;
}

// --- Users ---

export async function createUser(userData: Record<string, unknown>): Promise<Record<string, unknown>> {
  const { data } = await api.post('/api/v1/users', userData);
  return data;
}

export async function getCurrentUser(params?: { refresh_tier?: boolean }): Promise<Record<string, unknown>> {
  const { data } = await api.get('/api/v1/users/me', { params });
  return data;
}

export async function getPreferences(): Promise<Record<string, unknown> | null> {
  try {
    const { data } = await api.get('/api/v1/users/me/preferences');
    return data;
  } catch (e: unknown) {
    const err = e as { response?: { status?: number; data?: unknown }; message?: string };
    if (err.response?.status === 404) return null;
    throw e;
  }
}

export async function updateCurrentUser(userData: Record<string, unknown>): Promise<Record<string, unknown>> {
  const { data } = await api.put('/api/v1/users/me', userData);
  return data;
}

export async function updatePreferences(preferences: Record<string, unknown>): Promise<Record<string, unknown>> {
  const { data } = await api.put('/api/v1/users/me/preferences', preferences);
    return data;
}

export async function clearPreferences(): Promise<Record<string, unknown>> {
  const { data } = await api.delete('/api/v1/users/me/preferences');
  return data;
}

export async function uploadAvatar(file: File): Promise<{ avatar_url: string }> {
  const formData = new FormData();
  formData.append('file', file);

  const { data } = await api.post('/api/v1/users/me/avatar', formData);
  return data; // { avatar_url: "https://..." }
}

// --- Models & BYOK API Keys (moved to shared api/model.ts) ---

export { getAvailableModels, getUserApiKeys, updateUserApiKeys, deleteUserApiKey } from '@/api/model';

// --- OAuth (Connected Accounts) ---

/**
 * Start Codex device code flow — returns { user_code, verification_url, interval }.
 * POST /api/v1/oauth/codex/device/initiate
 */
export async function initiateCodexDevice(): Promise<Record<string, unknown>> {
  const { data } = await api.post('/api/v1/oauth/codex/device/initiate');
  return data;
}

/**
 * Poll for device authorization approval.
 * POST /api/v1/oauth/codex/device/poll
 * @returns {Promise<Object>} { pending: true } or { success: true, email, plan_type, account_id }
 */
export async function pollCodexDevice(): Promise<Record<string, unknown>> {
  const { data } = await api.post('/api/v1/oauth/codex/device/poll');
  return data;
}

/**
 * Check Codex OAuth connection status.
 * GET /api/v1/oauth/codex/status
 * Returns { connected, account_id, email, plan_type }
 */
export async function getCodexOAuthStatus(): Promise<{ connected: boolean; account_id: string | null; email: string | null; plan_type: string | null }> {
  try {
    const { data } = await api.get('/api/v1/oauth/codex/status');
    return data;
  } catch {
    return { connected: false, account_id: null, email: null, plan_type: null };
  }
}

/**
 * Disconnect Codex OAuth — delete stored tokens.
 * DELETE /api/v1/oauth/codex
 */
export async function disconnectCodexOAuth(): Promise<Record<string, unknown>> {
  const { data } = await api.delete('/api/v1/oauth/codex');
  return data;
}

// --- Grok OAuth (xAI device code flow) ---

/**
 * Start the xAI device code flow — returns { user_code, verification_url, interval }.
 * POST /api/v1/oauth/grok/device/initiate
 */
export async function initiateGrokDevice(): Promise<Record<string, unknown>> {
  const { data } = await api.post('/api/v1/oauth/grok/device/initiate');
  return data;
}

/**
 * Poll the xAI device authorization.
 * POST /api/v1/oauth/grok/device/poll
 */
export async function pollGrokDevice(): Promise<Record<string, unknown>> {
  const { data } = await api.post('/api/v1/oauth/grok/device/poll');
  return data;
}

/** Check Grok OAuth connection status. GET /api/v1/oauth/grok/status */
export async function getGrokOAuthStatus(): Promise<{ connected: boolean; account_id: string | null; email: string | null; plan_type: string | null }> {
  try {
    const { data } = await api.get('/api/v1/oauth/grok/status');
    return data || { connected: false, account_id: null, email: null, plan_type: null };
  } catch {
    return { connected: false, account_id: null, email: null, plan_type: null };
  }
}

/** Disconnect Grok OAuth — delete stored tokens. DELETE /api/v1/oauth/grok */
export async function disconnectGrokOAuth(): Promise<Record<string, unknown>> {
  const { data } = await api.delete('/api/v1/oauth/grok');
  return data;
}

// --- Gemini OAuth (Google account, Gemini Code Assist) ---

/**
 * Start the Gemini OAuth flow — returns { authorize_url }.
 * POST /api/v1/oauth/gemini/initiate
 */
export async function initiateGeminiOAuth(): Promise<Record<string, unknown>> {
  const { data } = await api.post('/api/v1/oauth/gemini/initiate');
  return data;
}

/**
 * Submit the Google authorization code pasted back by the user.
 * POST /api/v1/oauth/gemini/callback
 */
export async function submitGeminiCallback(callbackInput: string): Promise<Record<string, unknown>> {
  const { data } = await api.post('/api/v1/oauth/gemini/callback', { callback_input: callbackInput });
  return data;
}

/** Check Gemini OAuth connection status. GET /api/v1/oauth/gemini/status */
export async function getGeminiOAuthStatus(): Promise<{ connected: boolean; account_id: string | null; email: string | null; plan_type: string | null }> {
  try {
    const { data } = await api.get('/api/v1/oauth/gemini/status');
    return data || { connected: false, account_id: null, email: null, plan_type: null };
  } catch {
    return { connected: false, account_id: null, email: null, plan_type: null };
  }
}

/** Disconnect Gemini OAuth — delete stored tokens. DELETE /api/v1/oauth/gemini */
export async function disconnectGeminiOAuth(): Promise<Record<string, unknown>> {
  const { data } = await api.delete('/api/v1/oauth/gemini');
  return data;
}

// --- Claude OAuth (PKCE Authorization Code Flow) ---

/**
 * Initiate Claude OAuth — returns { authorize_url }.
 * POST /api/v1/oauth/claude/initiate
 */
export async function initiateClaudeOAuth(): Promise<Record<string, unknown>> {
  const { data } = await api.post('/api/v1/oauth/claude/initiate');
  return data;
}

/**
 * Submit Claude OAuth callback — exchange code#state for tokens.
 * POST /api/v1/oauth/claude/callback
 * @param {string} callbackInput - Full URL, code#state, or code=X&state=Y
 * @returns {Promise<Object>} { success: true }
 */
export async function submitClaudeCallback(callbackInput: string): Promise<Record<string, unknown>> {
  const { data } = await api.post('/api/v1/oauth/claude/callback', { callback_input: callbackInput });
  return data;
}

/**
 * Check Claude OAuth connection status.
 * GET /api/v1/oauth/claude/status
 * Returns { connected, account_id, email, plan_type }
 */
export async function getClaudeOAuthStatus(): Promise<{ connected: boolean; account_id: string | null; email: string | null; plan_type: string | null }> {
  try {
    const { data } = await api.get('/api/v1/oauth/claude/status');
    return data;
  } catch {
    return { connected: false, account_id: null, email: null, plan_type: null };
  }
}

/**
 * Disconnect Claude OAuth — delete stored tokens.
 * DELETE /api/v1/oauth/claude
 */
export async function disconnectClaudeOAuth(): Promise<Record<string, unknown>> {
  const { data } = await api.delete('/api/v1/oauth/claude');
  return data;
}

// --- Kimi OAuth (Anthropic-compatible coding plan, device code flow) ---

/** What `/oauth/kimi/device/initiate` hands back for the user to approve. */
export interface KimiDeviceInitiateResponse {
  user_code: string;
  verification_url: string;
  interval?: number;
  expires_in?: number;
}

/** A Kimi device poll either still waits for approval or reports the account. */
export type KimiDevicePollResponse =
  | { pending: true }
  | { success: true; email: string | null; account_id: string | null };

export interface KimiOAuthStatus {
  connected: boolean;
  account_id: string | null;
  email: string | null;
  plan_type: string | null;
}

/**
 * Start the Kimi device code flow.
 * POST /api/v1/oauth/kimi/device/initiate
 */
export async function initiateKimiDevice(): Promise<KimiDeviceInitiateResponse> {
  const { data } = await api.post('/api/v1/oauth/kimi/device/initiate');
  return data;
}

/**
 * Poll for Kimi device authorization approval.
 * POST /api/v1/oauth/kimi/device/poll
 * Resolves `{ pending: true }` until the user approves on the verification page.
 */
export async function pollKimiDevice(): Promise<KimiDevicePollResponse> {
  const { data } = await api.post('/api/v1/oauth/kimi/device/poll');
  return data;
}

/** Check Kimi OAuth connection status. GET /api/v1/oauth/kimi/status */
export async function getKimiOAuthStatus(): Promise<KimiOAuthStatus> {
  try {
    const { data } = await api.get('/api/v1/oauth/kimi/status');
    return (data as KimiOAuthStatus) || { connected: false, account_id: null, email: null, plan_type: null };
  } catch {
    return { connected: false, account_id: null, email: null, plan_type: null };
  }
}

/** Disconnect Kimi OAuth — delete stored tokens. DELETE /api/v1/oauth/kimi */
export async function disconnectKimiOAuth(): Promise<Record<string, unknown>> {
  const { data } = await api.delete('/api/v1/oauth/kimi');
  return data;
}

// --- Zhipu OAuth (Z.ai / BigModel, browser redirect + state poll) ---

/** BigModel is the China console; Z.ai is the international one. */
export type ZhipuProvider = 'zai' | 'bigmodel';

export interface ZhipuInitiateResponse {
  authorize_url: string;
  state: string;
  interval?: number;
  expires_in?: number;
}

export type ZhipuPollResponse =
  | { pending: true }
  | { success: true; provider: ZhipuProvider; email: string | null };

export interface ZhipuOAuthStatus {
  connected: boolean;
  provider: ZhipuProvider | null;
  email: string | null;
}

/**
 * Start the Zhipu/Z.ai OAuth flow.
 * POST /api/v1/oauth/zhipu/initiate
 *
 * `redirectUri` is sent for BigModel only: its console only registers a
 * loopback redirect back to `/oauth/zhipu`, and the server rejects any other
 * origin, which the caller surfaces as its own copy.
 */
export async function initiateZhipuOAuth(
  provider: ZhipuProvider,
  redirectUri?: string,
): Promise<ZhipuInitiateResponse> {
  const body: { provider: ZhipuProvider; redirect_uri?: string } = { provider };
  if (redirectUri) body.redirect_uri = redirectUri;
  const { data } = await api.post('/api/v1/oauth/zhipu/initiate', body);
  return data;
}

/**
 * Start the DeepSeek account sign-in. The platform answers with the authorize
 * URL the browser should open; the server keeps the PKCE verifier and state.
 * POST /api/v1/oauth/deepseek/initiate
 */
export async function initiateDeepSeekOAuth(
  redirectUri: string,
): Promise<{ authorize_url: string }> {
  const { data } = await api.post('/api/v1/oauth/deepseek/initiate', {
    redirect_uri: redirectUri,
  });
  return data;
}

/**
 * Hand the browser's code/state pair to the server, which performs the
 * auth_exchange and stores the account token.
 * POST /api/v1/oauth/deepseek/callback
 */
export async function submitDeepSeekCallback(
  code: string,
  state: string,
): Promise<{ success: boolean }> {
  const { data } = await api.post('/api/v1/oauth/deepseek/callback', { code, state });
  return data;
}

/** Whether this user has a stored DeepSeek account token. */
export async function getDeepSeekOAuthStatus(): Promise<{ connected: boolean }> {
  const { data } = await api.get('/api/v1/oauth/deepseek/status');
  return data;
}

/** One row of the signed-in account's Platform model catalog. */
export interface DeepSeekAccountModel {
  id?: string;
  name?: string;
  display_name?: string;
  context_window?: number;
  [key: string]: unknown;
}

/** The account's Platform model catalog. GET /api/v1/oauth/deepseek/models */
export async function getDeepSeekOAuthModels(): Promise<{
  models: DeepSeekAccountModel[];
}> {
  const { data } = await api.get('/api/v1/oauth/deepseek/models');
  return data;
}

/** Forget the stored DeepSeek account token (local only, best-effort logout). */
export async function disconnectDeepSeekOAuth(): Promise<{ success: boolean }> {
  const { data } = await api.delete('/api/v1/oauth/deepseek');
  return data;
}

/**
 * Poll the browser sign-in by the `state` `initiate` returned.
 * POST /api/v1/oauth/zhipu/poll
 */
export async function pollZhipuOAuth(state: string): Promise<ZhipuPollResponse> {
  const { data } = await api.post('/api/v1/oauth/zhipu/poll', { state });
  return data;
}

/**
 * Exchange the BigModel redirect's code for tokens. Called by the
 * `/oauth/zhipu` callback page; `redirectUri` must be exactly the one the flow
 * was initiated with.
 * POST /api/v1/oauth/zhipu/callback
 */
export async function submitZhipuCallback(
  code: string,
  state: string,
  redirectUri: string,
): Promise<{ success: true; provider: ZhipuProvider; email: string | null }> {
  const { data } = await api.post('/api/v1/oauth/zhipu/callback', {
    code,
    state,
    redirect_uri: redirectUri,
  });
  return data;
}

/** Check Zhipu OAuth connection status. GET /api/v1/oauth/zhipu/status */
export async function getZhipuOAuthStatus(): Promise<ZhipuOAuthStatus> {
  try {
    const { data } = await api.get('/api/v1/oauth/zhipu/status');
    return (data as ZhipuOAuthStatus) || { connected: false, provider: null, email: null };
  } catch {
    return { connected: false, provider: null, email: null };
  }
}

/** Disconnect the Zhipu connection (either provider). DELETE /api/v1/oauth/zhipu */
export async function disconnectZhipuOAuth(): Promise<Record<string, unknown>> {
  const { data } = await api.delete('/api/v1/oauth/zhipu');
  return data;
}

// --- MiMo OAuth (browser authorization + encrypted paste-back code) ---

/** Where `/oauth/mimo/initiate` sends the user to sign in. */
export interface MimoInitiateResponse {
  authorize_url: string;
}

/** What a successful callback reports: the MiMo account whose key was minted. */
export interface MimoCallbackResponse {
  success: true;
  uid: string;
  base_url_host: string | null;
}

export interface MimoOAuthStatus {
  connected: boolean;
  account_id: string | null;
  email: string | null;
  plan_type: string | null;
}

/**
 * Start the MiMo sign-in; the server keeps the one-time pending flow for this
 * user, so the response is only the page to open.
 * POST /api/v1/oauth/mimo/initiate
 */
export async function initiateMimoOAuth(): Promise<MimoInitiateResponse> {
  const { data } = await api.post('/api/v1/oauth/mimo/initiate');
  return data;
}

/**
 * Exchange the code the MiMo authorize page shows for the minted coding-plan
 * key. POST /api/v1/oauth/mimo/callback
 */
export async function submitMimoCallback(code: string): Promise<MimoCallbackResponse> {
  const { data } = await api.post('/api/v1/oauth/mimo/callback', { code });
  return data;
}

/** Check MiMo connection status. GET /api/v1/oauth/mimo/status */
export async function getMimoOAuthStatus(): Promise<MimoOAuthStatus> {
  try {
    const { data } = await api.get('/api/v1/oauth/mimo/status');
    return (data as MimoOAuthStatus) || { connected: false, account_id: null, email: null, plan_type: null };
  } catch {
    return { connected: false, account_id: null, email: null, plan_type: null };
  }
}

/** Disconnect MiMo — delete the minted key. DELETE /api/v1/oauth/mimo */
export async function disconnectMimoOAuth(): Promise<Record<string, unknown>> {
  const { data } = await api.delete('/api/v1/oauth/mimo');
  return data;
}

// --- Connection capability catalog ---

/**
 * One connection the UI can render: a subscription sign-in (device / paste /
 * redirect), or a provider that only takes an API key. `unavailable_reason`
 * explains the latter so the UI never invents copy.
 */
export interface OAuthProviderDescriptor {
  id: string;
  /** Display name in the server's primary language (zh). */
  display_name: string;
  display_name_en: string;
  brand: string;
  /** Endpoint geography, not the section a card belongs to. */
  region: 'cn' | 'intl';
  kind: 'device' | 'paste' | 'redirect' | 'api_key_only';
  login_supported: boolean;
  unavailable_reason: string | null;
  unavailable_reason_en: string | null;
  /** Coding/token-plan caveats, when the route is a subscription plan. */
  usage_hint?: string | null;
  usage_hint_en?: string | null;
  usage_policy_url?: string | null;
  /** The BYOK provider id the "enter an API key" action opens. */
  api_key_provider: string | null;
  website_url: string | null;
  models: string[];
  order: number;
  /** Explicit API-key/plan routes for this provider family. */
  connect_options?: Array<{
    provider: string;
    display_name: string;
    access_type: 'api_key' | 'coding_plan';
    plan_kind?: 'token' | 'agent' | null;
    plan_name?: string | null;
    sdk?: string | null;
    base_url?: string | null;
    use_response_api?: boolean;
    region?: string;
    dynamic_models?: boolean;
  }>;
  /** SDK/Base URL/region defaults for the API-key fallback editor. */
  connect_defaults?: {
    sdk?: string | null;
    base_url?: string | null;
    use_response_api?: boolean;
    region?: string | null;
    dynamic_models?: boolean;
    region_variants?: Array<{
      provider: string;
      display_name: string;
      region: string;
      sdk?: string | null;
      base_url?: string | null;
      use_response_api?: boolean;
    }>;
  } | null;
}

export interface OAuthProvidersResponse {
  providers: OAuthProviderDescriptor[];
}

/**
 * Every connection the backend can offer, in UI order. The connection surfaces
 * render from this instead of a hard-coded card list.
 * GET /api/v1/oauth/providers
 */
export async function getOAuthProviders(): Promise<OAuthProvidersResponse> {
  const { data } = await api.get('/api/v1/oauth/providers');
  return data;
}
