/**
 * The shared subscription-login state machine: the flows Settings and Setup
 * both drive. The API module is mocked, so what is pinned here is the hook's
 * behaviour — start, poll, timeout, cache refresh, cancel and disconnect —
 * not the implementation's shape.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from '@testing-library/react';
import { renderHookWithProviders } from '@/test/utils';

const api = vi.hoisted(() => ({
  initiateCodexDevice: vi.fn(),
  pollCodexDevice: vi.fn(),
  disconnectCodexOAuth: vi.fn(),
  initiateGrokDevice: vi.fn(),
  pollGrokDevice: vi.fn(),
  disconnectGrokOAuth: vi.fn(),
  initiateKimiDevice: vi.fn(),
  pollKimiDevice: vi.fn(),
  disconnectKimiOAuth: vi.fn(),
  initiateClaudeOAuth: vi.fn(),
  submitClaudeCallback: vi.fn(),
  disconnectClaudeOAuth: vi.fn(),
  initiateGeminiOAuth: vi.fn(),
  submitGeminiCallback: vi.fn(),
  disconnectGeminiOAuth: vi.fn(),
  initiateZhipuOAuth: vi.fn(),
  pollZhipuOAuth: vi.fn(),
  disconnectZhipuOAuth: vi.fn(),
  initiateMimoOAuth: vi.fn(),
  submitMimoCallback: vi.fn(),
  disconnectMimoOAuth: vi.fn(),
  initiateDeepSeekOAuth: vi.fn(),
  getDeepSeekOAuthStatus: vi.fn(),
  disconnectDeepSeekOAuth: vi.fn(),
}));

vi.mock('@/lib/dashboardApi/api', () => api);

import { useSubscriptionLogin, zhipuRedirectUri } from '../useSubscriptionLogin';

/** Let real timers (the polling interval) run inside act, so the state updates
 *  the interval causes are attributed to the test action rather than warned about. */
const letPollingRun = (ms: number) =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(window, 'open').mockReturnValue(null);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('device-code login (Kimi)', () => {
  it('goes from pending to connected and refreshes the caches', async () => {
    api.initiateKimiDevice.mockResolvedValue({
      user_code: 'ABCD-EFGH',
      verification_url: 'https://kimi.example/device',
      interval: 0.01,
    });
    api.pollKimiDevice
      .mockResolvedValueOnce({ pending: true })
      .mockResolvedValue({ success: true, email: 'kimi@example.com', account_id: 'acct-1' });
    const onConnected = vi.fn();
    const { result, queryClient } = renderHookWithProviders(() =>
      useSubscriptionLogin('kimi-oauth', { onConnected }),
    );
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');

    await act(async () => {
      await result.current.start();
    });

    expect(result.current.awaiting).toBe(true);
    expect(result.current.deviceCode).toEqual({
      user_code: 'ABCD-EFGH',
      verification_url: 'https://kimi.example/device',
    });
    expect(window.open).toHaveBeenCalledWith('https://kimi.example/device', '_blank', 'noopener');

    await letPollingRun(60);
    expect(onConnected).toHaveBeenCalledTimes(1);
    expect(api.pollKimiDevice).toHaveBeenCalledTimes(2);
    expect(onConnected.mock.calls[0][0]).toMatchObject({
      email: 'kimi@example.com',
      account_id: 'acct-1',
      plan_type: null,
    });
    expect(invalidate).toHaveBeenCalled();
    expect(result.current.deviceCode).toBeNull();
    expect(result.current.awaiting).toBe(false);
  });

  it('reports a timeout when approval never arrives', async () => {
    api.initiateKimiDevice.mockResolvedValue({
      user_code: 'ABCD-EFGH',
      verification_url: 'https://kimi.example/device',
      interval: 0.001,
    });
    api.pollKimiDevice.mockResolvedValue({ pending: true });
    const { result } = renderHookWithProviders(() =>
      useSubscriptionLogin('kimi-oauth', { timeoutMs: 20 }),
    );

    await act(async () => {
      await result.current.start();
    });

    await letPollingRun(80);
    expect(result.current.error).toBe('Authorization timed out. Please try again.');
    expect(result.current.polling).toBe(false);
    expect(api.pollKimiDevice).toHaveBeenCalled();
  });

  it('reports a poll failure instead of polling forever', async () => {
    api.initiateKimiDevice.mockResolvedValue({
      user_code: 'ABCD-EFGH',
      verification_url: 'https://kimi.example/device',
      interval: 0.005,
    });
    api.pollKimiDevice.mockRejectedValue(new Error('network down'));
    const { result } = renderHookWithProviders(() => useSubscriptionLogin('kimi-oauth'));

    await act(async () => {
      await result.current.start();
    });

    await letPollingRun(40);
    expect(result.current.error).toBe(
      'Something went wrong while waiting for authorization. Please try again.',
    );
    expect(result.current.polling).toBe(false);
  });

  it('reports a start failure when initiation is rejected', async () => {
    api.initiateKimiDevice.mockRejectedValue(new Error('502'));
    const { result } = renderHookWithProviders(() => useSubscriptionLogin('kimi-oauth'));

    await act(async () => {
      await result.current.start();
    });

    expect(result.current.error).toBe('Failed to start the sign-in. Please try again.');
    expect(result.current.awaiting).toBe(false);
    expect(result.current.starting).toBe(false);
  });
});

describe('browser sign-in (Zhipu)', () => {
  it('drives the BigModel flow with a loopback redirect and by-state polling', async () => {
    api.initiateZhipuOAuth.mockResolvedValue({
      authorize_url: 'https://bigmodel.example/auth',
      state: 'state-1',
      interval: 0.01,
    });
    api.pollZhipuOAuth
      .mockResolvedValueOnce({ pending: true })
      .mockResolvedValue({ success: true, provider: 'bigmodel', email: 'z@example.com' });
    const onConnected = vi.fn();
    const { result } = renderHookWithProviders(() =>
      useSubscriptionLogin('z-ai-cn-oauth', { onConnected }),
    );

    await act(async () => {
      await result.current.start();
    });

    expect(api.initiateZhipuOAuth).toHaveBeenCalledWith(
      'bigmodel',
      expect.stringMatching(/\/oauth\/zhipu$/),
    );
    expect(result.current.authorizeUrl).toBe('https://bigmodel.example/auth');
    expect(window.open).toHaveBeenCalledWith('https://bigmodel.example/auth', '_blank', 'noopener');

    await letPollingRun(60);
    expect(onConnected).toHaveBeenCalledTimes(1);
    expect(api.pollZhipuOAuth.mock.calls).toEqual([['state-1'], ['state-1']]);
    expect(onConnected.mock.calls[0][0]).toMatchObject({
      email: 'z@example.com',
      provider: 'bigmodel',
    });
  });

  it('starts the Z.ai flow without a redirect_uri', async () => {
    api.initiateZhipuOAuth.mockResolvedValue({
      authorize_url: 'https://z.ai/auth',
      state: 'state-2',
      interval: 0.01,
    });
    api.pollZhipuOAuth.mockResolvedValue({ pending: true });
    const { result } = renderHookWithProviders(() => useSubscriptionLogin('z-ai-oauth'));

    await act(async () => {
      await result.current.start();
    });

    expect(api.initiateZhipuOAuth).toHaveBeenCalledWith('zai', undefined);
    expect(result.current.awaiting).toBe(true);

    act(() => result.current.cancel());
    expect(result.current.awaiting).toBe(false);
    expect(result.current.authorizeUrl).toBeNull();
  });

  it('explains a rejected BigModel redirect as needing loopback', async () => {
    api.initiateZhipuOAuth.mockRejectedValue({ response: { status: 400 } });
    const { result } = renderHookWithProviders(() => useSubscriptionLogin('z-ai-cn-oauth'));

    await act(async () => {
      await result.current.start();
    });

    expect(result.current.error).toBe(
      'Zhipu sign-in only works when the app is opened on localhost. This address is not supported.',
    );
  });

  it('normalizes a loopback origin to the localhost spelling BigModel registers', () => {
    expect(zhipuRedirectUri()).toBe('http://localhost:3000/oauth/zhipu');
  });
});

describe('paste-back login (Claude)', () => {
  it('opens the authorize page and exchanges the pasted code', async () => {
    api.initiateClaudeOAuth.mockResolvedValue({ authorize_url: 'https://claude.example/authorize' });
    api.submitClaudeCallback.mockResolvedValue({
      success: true,
      email: 'claude@example.com',
      account_id: 'acct-c',
      plan_type: 'pro',
    });
    const onConnected = vi.fn();
    const { result } = renderHookWithProviders(() =>
      useSubscriptionLogin('claude-oauth', { onConnected }),
    );

    await act(async () => {
      await result.current.start();
    });
    expect(result.current.authorizeUrl).toBe('https://claude.example/authorize');

    let completed = false;
    await act(async () => {
      completed = await result.current.submit('code#state');
    });

    expect(completed).toBe(true);
    expect(api.submitClaudeCallback).toHaveBeenCalledWith('code#state');
    expect(onConnected.mock.calls[0][0]).toMatchObject({ email: 'claude@example.com' });
  });

  it('reports a rejected code without leaving the active phase', async () => {
    api.initiateClaudeOAuth.mockResolvedValue({ authorize_url: 'https://claude.example/authorize' });
    api.submitClaudeCallback.mockRejectedValue(new Error('invalid_grant'));
    const { result } = renderHookWithProviders(() => useSubscriptionLogin('claude-oauth'));

    await act(async () => {
      await result.current.start();
    });
    let completed = true;
    await act(async () => {
      completed = await result.current.submit('bad-code');
    });

    expect(completed).toBe(false);
    expect(result.current.error).toBe('That authorization code was not accepted. Please try again.');
    expect(result.current.awaiting).toBe(true);
  });
});

describe('paste-back login (MiMo)', () => {
  it('opens the MiMo authorize page and exchanges the pasted code', async () => {
    api.initiateMimoOAuth.mockResolvedValue({ authorize_url: 'https://mimo.example/authorize' });
    api.submitMimoCallback.mockResolvedValue({
      success: true,
      uid: 'mimo-user-1',
      base_url_host: 'api.mimo.example',
    });
    const onConnected = vi.fn();
    const { result } = renderHookWithProviders(() =>
      useSubscriptionLogin('mimo-oauth', { onConnected }),
    );

    await act(async () => {
      await result.current.start();
    });

    expect(result.current.kind).toBe('paste');
    expect(result.current.authorizeUrl).toBe('https://mimo.example/authorize');
    expect(result.current.awaiting).toBe(true);
    expect(window.open).toHaveBeenCalledWith(
      'https://mimo.example/authorize',
      '_blank',
      'noopener',
    );

    let completed = false;
    await act(async () => {
      completed = await result.current.submit('u-encrypted-code');
    });

    expect(completed).toBe(true);
    expect(api.submitMimoCallback).toHaveBeenCalledWith('u-encrypted-code');
    expect(result.current.awaiting).toBe(false);
    expect(onConnected).toHaveBeenCalledTimes(1);
  });

  it('reports a rejected code without leaving the active phase', async () => {
    api.initiateMimoOAuth.mockResolvedValue({ authorize_url: 'https://mimo.example/authorize' });
    api.submitMimoCallback.mockRejectedValue(new Error('invalid code'));
    const { result } = renderHookWithProviders(() => useSubscriptionLogin('mimo-oauth'));

    await act(async () => {
      await result.current.start();
    });
    let completed = true;
    await act(async () => {
      completed = await result.current.submit('bad-code');
    });

    expect(completed).toBe(false);
    expect(result.current.error).toBe('That authorization code was not accepted. Please try again.');
    expect(result.current.awaiting).toBe(true);
  });

  it('clears the connection through the MiMo endpoint', async () => {
    api.disconnectMimoOAuth.mockResolvedValue({ success: true });
    const { result } = renderHookWithProviders(() => useSubscriptionLogin('mimo-oauth'));

    await act(async () => {
      await result.current.disconnect();
    });

    expect(api.disconnectMimoOAuth).toHaveBeenCalledTimes(1);
  });
});

describe('disconnect', () => {
  it('clears the Kimi connection through its own endpoint', async () => {
    api.disconnectKimiOAuth.mockResolvedValue({ success: true });
    const { result, queryClient } = renderHookWithProviders(() => useSubscriptionLogin('kimi-oauth'));
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');

    await act(async () => {
      await result.current.disconnect();
    });

    expect(api.disconnectKimiOAuth).toHaveBeenCalledTimes(1);
    expect(invalidate).toHaveBeenCalled();
    expect(result.current.disconnecting).toBe(false);
  });

  it('clears either Zhipu console through the shared endpoint', async () => {
    api.disconnectZhipuOAuth.mockResolvedValue({ success: true });
    const { result } = renderHookWithProviders(() => useSubscriptionLogin('z-ai-oauth'));

    await act(async () => {
      await result.current.disconnect();
    });

    expect(api.disconnectZhipuOAuth).toHaveBeenCalledTimes(1);
  });
});

describe('provider coverage', () => {
  it('tells the user when a provider has no flow in this build', async () => {
    const { result } = renderHookWithProviders(() => useSubscriptionLogin('mystery-oauth'));

    await act(async () => {
      await result.current.start();
    });

    expect(result.current.kind).toBeNull();
    expect(result.current.error).toBe('This provider does not support subscription sign-in yet.');
  });
});


describe('DeepSeek account login', () => {
  it('opens the platform page and connects when the status poll lands', async () => {
    api.initiateDeepSeekOAuth.mockResolvedValue({
      authorize_url: 'https://platform.deepseek.com/authorize',
    });
    api.getDeepSeekOAuthStatus
      .mockResolvedValueOnce({ connected: false })
      .mockResolvedValue({ connected: true });
    const onConnected = vi.fn();
    const { result } = renderHookWithProviders(() =>
      useSubscriptionLogin('deepseek-account', { onConnected, pollIntervalMs: 5 }),
    );

    await act(async () => {
      await result.current.start();
    });

    expect(api.initiateDeepSeekOAuth).toHaveBeenCalledWith(
      expect.stringContaining('/oauth/callback'),
    );
    expect(window.open).toHaveBeenCalledWith(
      'https://platform.deepseek.com/authorize',
      '_blank',
      'noopener',
    );
    expect(result.current.kind).toBe('deepseek');
    expect(result.current.awaiting).toBe(true);

    await letPollingRun(80);
    expect(onConnected).toHaveBeenCalledTimes(1);
    expect(result.current.awaiting).toBe(false);
  });
});
