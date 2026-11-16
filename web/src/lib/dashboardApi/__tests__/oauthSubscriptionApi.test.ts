// @vitest-environment node
/**
 * The endpoint contract for the subscription logins is shared with a backend
 * built in parallel, so the paths and bodies are pinned here rather than left
 * to be rediscovered by a 404 in the UI.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('@/api/client', () => ({ api: apiMock }));

import {
  disconnectKimiOAuth,
  disconnectMimoOAuth,
  disconnectZhipuOAuth,
  getKimiOAuthStatus,
  getMimoOAuthStatus,
  getOAuthProviders,
  getZhipuOAuthStatus,
  initiateKimiDevice,
  initiateMimoOAuth,
  initiateZhipuOAuth,
  pollKimiDevice,
  pollZhipuOAuth,
  submitMimoCallback,
  submitZhipuCallback,
} from '../api';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('Kimi device login contract', () => {
  it('initiates, polls and disconnects on the documented paths', async () => {
    apiMock.post.mockResolvedValueOnce({
      data: { user_code: 'ABCD-EFGH', verification_url: 'https://kimi.example/device', interval: 5, expires_in: 900 },
    });
    await expect(initiateKimiDevice()).resolves.toMatchObject({
      user_code: 'ABCD-EFGH',
      verification_url: 'https://kimi.example/device',
    });
    expect(apiMock.post).toHaveBeenNthCalledWith(1, '/api/v1/oauth/kimi/device/initiate');

    apiMock.post.mockResolvedValueOnce({ data: { pending: true } });
    await expect(pollKimiDevice()).resolves.toEqual({ pending: true });
    expect(apiMock.post).toHaveBeenNthCalledWith(2, '/api/v1/oauth/kimi/device/poll');

    apiMock.delete.mockResolvedValueOnce({ data: { success: true } });
    await expect(disconnectKimiOAuth()).resolves.toEqual({ success: true });
    expect(apiMock.delete).toHaveBeenCalledWith('/api/v1/oauth/kimi');
  });

  it('reads the status and treats a missing endpoint as disconnected', async () => {
    apiMock.get.mockResolvedValueOnce({
      data: { connected: true, account_id: 'acct', email: 'kimi@example.com', plan_type: 'pro' },
    });
    await expect(getKimiOAuthStatus()).resolves.toMatchObject({ connected: true, email: 'kimi@example.com' });
    expect(apiMock.get).toHaveBeenCalledWith('/api/v1/oauth/kimi/status');

    apiMock.get.mockRejectedValueOnce(new Error('404'));
    await expect(getKimiOAuthStatus()).resolves.toEqual({
      connected: false, account_id: null, email: null, plan_type: null,
    });
  });
});

describe('Zhipu login contract', () => {
  it('sends BigModel a redirect_uri and Z.ai none', async () => {
    apiMock.post.mockResolvedValue({ data: { authorize_url: 'https://zhipu.example/auth', state: 'st-1' } });

    await initiateZhipuOAuth('bigmodel', 'http://localhost:5173/oauth/zhipu');
    expect(apiMock.post).toHaveBeenLastCalledWith('/api/v1/oauth/zhipu/initiate', {
      provider: 'bigmodel',
      redirect_uri: 'http://localhost:5173/oauth/zhipu',
    });

    await initiateZhipuOAuth('zai');
    expect(apiMock.post).toHaveBeenLastCalledWith('/api/v1/oauth/zhipu/initiate', { provider: 'zai' });
  });

  it('polls by state and exchanges the redirect code with the same redirect_uri', async () => {
    apiMock.post.mockResolvedValueOnce({ data: { pending: true } });
    await expect(pollZhipuOAuth('st-1')).resolves.toEqual({ pending: true });
    expect(apiMock.post).toHaveBeenLastCalledWith('/api/v1/oauth/zhipu/poll', { state: 'st-1' });

    apiMock.post.mockResolvedValueOnce({ data: { success: true, provider: 'bigmodel', email: 'z@example.com' } });
    await expect(submitZhipuCallback('code-1', 'st-1', 'http://localhost:5173/oauth/zhipu')).resolves.toMatchObject({
      success: true,
      provider: 'bigmodel',
    });
    expect(apiMock.post).toHaveBeenLastCalledWith('/api/v1/oauth/zhipu/callback', {
      code: 'code-1',
      state: 'st-1',
      redirect_uri: 'http://localhost:5173/oauth/zhipu',
    });
  });

  it('reads the status and treats a missing endpoint as disconnected', async () => {
    apiMock.get.mockResolvedValueOnce({
      data: { connected: true, provider: 'zai', email: 'zai@example.com' },
    });
    await expect(getZhipuOAuthStatus()).resolves.toMatchObject({ connected: true, provider: 'zai' });
    expect(apiMock.get).toHaveBeenCalledWith('/api/v1/oauth/zhipu/status');

    apiMock.get.mockRejectedValueOnce(new Error('404'));
    await expect(getZhipuOAuthStatus()).resolves.toEqual({ connected: false, provider: null, email: null });
  });

  it('disconnects through the one Zhipu endpoint', async () => {
    apiMock.delete.mockResolvedValueOnce({ data: { success: true } });
    await expect(disconnectZhipuOAuth()).resolves.toEqual({ success: true });
    expect(apiMock.delete).toHaveBeenCalledWith('/api/v1/oauth/zhipu');
  });
});

describe('MiMo paste-code login contract', () => {
  it('initiates, exchanges the pasted code and disconnects on the documented paths', async () => {
    apiMock.post.mockResolvedValueOnce({
      data: { authorize_url: 'https://platform.example/authorize?pk=pub&kn=mimocode' },
    });
    await expect(initiateMimoOAuth()).resolves.toMatchObject({
      authorize_url: 'https://platform.example/authorize?pk=pub&kn=mimocode',
    });
    expect(apiMock.post).toHaveBeenNthCalledWith(1, '/api/v1/oauth/mimo/initiate');

    apiMock.post.mockResolvedValueOnce({
      data: { success: true, uid: 'mimo-user-1', base_url_host: 'api.mimo.example' },
    });
    await expect(submitMimoCallback('u-encrypted-code')).resolves.toMatchObject({
      success: true,
      uid: 'mimo-user-1',
    });
    expect(apiMock.post).toHaveBeenNthCalledWith(2, '/api/v1/oauth/mimo/callback', {
      code: 'u-encrypted-code',
    });

    apiMock.delete.mockResolvedValueOnce({ data: { success: true } });
    await expect(disconnectMimoOAuth()).resolves.toEqual({ success: true });
    expect(apiMock.delete).toHaveBeenCalledWith('/api/v1/oauth/mimo');
  });

  it('reads the status and treats a missing endpoint as disconnected', async () => {
    apiMock.get.mockResolvedValueOnce({
      data: { connected: true, account_id: null, email: null, plan_type: 'coding_plan' },
    });
    await expect(getMimoOAuthStatus()).resolves.toMatchObject({
      connected: true,
      plan_type: 'coding_plan',
    });
    expect(apiMock.get).toHaveBeenCalledWith('/api/v1/oauth/mimo/status');

    apiMock.get.mockRejectedValueOnce(new Error('404'));
    await expect(getMimoOAuthStatus()).resolves.toEqual({
      connected: false,
      account_id: null,
      email: null,
      plan_type: null,
    });
  });
});

describe('connection capability catalog contract', () => {
  it('reads every descriptor from the providers endpoint', async () => {
    const descriptor = {
      id: 'mimo-oauth',
      display_name: 'MiMo 订阅登录',
      display_name_en: 'MiMo subscription',
      brand: 'mimo',
      region: 'cn',
      kind: 'paste',
      login_supported: true,
      unavailable_reason: null,
      unavailable_reason_en: null,
      api_key_provider: 'mimo',
      website_url: 'https://platform.xiaomimimo.com/token-plan',
      models: ['mimo-v2.6-flash-oauth', 'mimo-v2.6-pro-oauth'],
      order: 20,
    };
    apiMock.get.mockResolvedValueOnce({ data: { providers: [descriptor] } });

    await expect(getOAuthProviders()).resolves.toEqual({ providers: [descriptor] });
    expect(apiMock.get).toHaveBeenCalledWith('/api/v1/oauth/providers');
  });
});
